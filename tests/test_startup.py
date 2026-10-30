"""Configuration and supervisor tests; no external model requests."""

import os
import signal
import socket
import subprocess
import sys

import pytest

from app.config import load_environment
from start import Service, StartupError, Supervisor, check_port, preflight, redact, services


def test_dotenv_location_precedence_and_literal_secret(tmp_path, monkeypatch):
    project = tmp_path / "项目 with spaces"
    project.mkdir()
    (project / ".env").write_text(
        'TEST_DOTENV_SECRET="literal-${NAME}-#-密钥"\nTEST_DOTENV_OVERRIDE=file\n',
        encoding="utf-8-sig")
    monkeypatch.delenv("TEST_DOTENV_SECRET", raising=False)
    monkeypatch.setenv("TEST_DOTENV_OVERRIDE", "terminal")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_DOTENV_SECRET=wrong-directory\n")
    load_environment(project)
    assert os.environ["TEST_DOTENV_SECRET"] == "literal-${NAME}-#-密钥"
    assert os.environ["TEST_DOTENV_OVERRIDE"] == "terminal"
    monkeypatch.delenv("TEST_DOTENV_SECRET")


def test_missing_dotenv_is_optional(tmp_path):
    load_environment(tmp_path)


def test_missing_key_rejected_before_any_services_start(monkeypatch):
    monkeypatch.setattr("start.importlib.util.find_spec", lambda _: object())
    with pytest.raises(StartupError, match="INTERN_API_KEY"):
        preflight(services(False), {"INTERN_API_KEY": "在这里填端砚key"}, False)


def test_offline_has_no_gateway_or_cli_dependency():
    assert [spec.name for spec in services(True)] == ["portal", "api"]


def test_logs_redact_secret_and_authorization():
    env = {"INTERN_API_KEY": "private-value-123", "OTHER_TOKEN": "token-value-456"}
    text = redact("key=private-value-123 token-value-456 Authorization: Bearer unknown-key", env)
    assert "private-value" not in text
    assert "token-value" not in text
    assert "unknown-key" not in text
    assert text.count("[REDACTED]") == 3


def test_busy_port_is_not_adopted_or_closed():
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(StartupError, match=str(port)):
            check_port(port)
        assert owner.fileno() != -1


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stub_service(tmp_path):
    path = tmp_path / "fake_service.py"
    path.write_text('''import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass
print(os.environ.get("INTERN_API_KEY", ""), flush=True)
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
''', encoding="utf-8")
    port = free_port()
    return Service("portal", port, [sys.executable, str(path), str(port)], "/health")


def test_real_supervisor_starts_logs_redacts_and_stops_only_owned_process(tmp_path):
    spec = stub_service(tmp_path)
    env = {**os.environ, "INTERN_API_KEY": "local-test-secret", "PYTHONIOENCODING": "utf-8"}
    supervisor = Supervisor([spec], env, tmp_path / "logs", timeout=10)
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    try:
        supervisor.start()
        assert supervisor.children[0][1].poll() is None
        supervisor.stop()
        assert supervisor.children[0][1].poll() is not None
        assert outsider.poll() is None
        content = (tmp_path / "logs/portal.log").read_text(encoding="utf-8")
        assert "local-test-secret" not in content
        assert "[REDACTED]" in content
        check_port(spec.port)
    finally:
        supervisor.stop()
        outsider.terminate()
        outsider.wait(timeout=5)


def test_partial_launch_failure_can_roll_back_started_service(tmp_path):
    first = stub_service(tmp_path)
    second = Service("broken", free_port(), [str(tmp_path / "missing-program")], "/health")
    supervisor = Supervisor([first, second], dict(os.environ), tmp_path / "logs", timeout=10)
    try:
        with pytest.raises(OSError):
            supervisor.start()
    finally:
        supervisor.stop()
    assert supervisor.children[0][1].poll() is not None
    check_port(first.port)


def test_exited_child_does_not_report_ready(tmp_path):
    spec = Service("portal", free_port(), [sys.executable, "-c", "raise SystemExit(3)"], "/health")
    supervisor = Supervisor([spec], dict(os.environ), tmp_path / "logs", timeout=5)
    try:
        with pytest.raises(StartupError, match="退出"):
            supervisor.start()
    finally:
        supervisor.stop()


def test_health_timeout_stops_unready_process(tmp_path):
    spec = Service("portal", free_port(), [sys.executable, "-c", "import time; time.sleep(30)"], "/health")
    supervisor = Supervisor([spec], dict(os.environ), tmp_path / "logs", timeout=0.1)
    try:
        with pytest.raises(StartupError, match="超时"):
            supervisor.start()
    finally:
        supervisor.stop()
    assert supervisor.children[0][1].poll() is not None


def test_entrypoint_rolls_back_partial_startup(tmp_path, monkeypatch):
    import start
    first = stub_service(tmp_path)
    second = Service("broken", free_port(), [str(tmp_path / "missing-program")], "/health")
    supervisor = Supervisor([first, second], dict(os.environ), tmp_path / "logs", timeout=10)
    monkeypatch.setattr(start, "services", lambda _: [first, second])
    monkeypatch.setattr(start, "Supervisor", lambda *args: supervisor)
    assert start.main(["--offline"]) == 1
    assert supervisor.children[0][1].poll() is not None
    check_port(first.port)


@pytest.mark.skipif(os.getenv("RUN_STARTUP_E2E") != "1", reason="opt-in real local services")
@pytest.mark.parametrize("offline", [True, False])
def test_real_entrypoint_health_status_and_ctrl_c(tmp_path, monkeypatch, offline):
    import start
    # Never use a real upstream key or the user's database for readiness checks.
    monkeypatch.setenv("INTERN_API_KEY", "sk-local-startup-test-not-real")
    monkeypatch.setenv("RESTOCK_AGENT_MODE", "off" if offline else "required")
    monkeypatch.setenv("PROCUREMENT_PORTAL_URL", "http://127.0.0.1:8200")
    monkeypatch.setenv("PROCUREMENT_DB_PATH", str(tmp_path / "portal.db"))
    monkeypatch.setenv("RESTOCK_DB_PATH", str(tmp_path / "main.db"))
    observed = []

    def ready_then_interrupt(self):
        assert start.main(["status"] + (["--offline"] if offline else [])) == 0
        observed.extend(proc for _, proc, _, _ in self.children)
        signal.raise_signal(signal.SIGINT)
        assert self.stopping.is_set()

    monkeypatch.setattr(Supervisor, "wait", ready_then_interrupt)
    assert start.main(["--offline"] if offline else []) == 0
    assert len(observed) == (2 if offline else 3)
    assert all(proc.poll() is not None for proc in observed)
    for spec in services(offline):
        check_port(spec.port)
