"""Single-console supervisor: python start.py [start|status] [--offline|--check]."""

import argparse
import importlib.util
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Service:
    name: str
    port: int
    command: list[str]
    health_path: str


class StartupError(RuntimeError):
    pass


def services(offline: bool) -> list[Service]:
    result = []
    if not offline:
        # Use the installed console entry point in this same Python environment.
        entry = (
            "from importlib.metadata import distribution; "
            "next(e for e in distribution('litellm').entry_points "
            "if e.group == 'console_scripts' and e.name == 'litellm').load()()"
        )
        result.append(Service("proxy", 4000, [sys.executable, "-c", entry,
            "--config", str(ROOT / "agent-integration/litellm-config.yaml"),
            "--host", "127.0.0.1", "--port", "4000"], "/v1/models"))
    for name, port, module in (
        ("portal", 8200, "app.procurement_portal:app"),
        ("api", 8100, "app.main:app"),
    ):
        result.append(Service(name, port, [sys.executable, "-m", "uvicorn",
            module, "--host", "127.0.0.1", "--port", str(port)], "/health"))
    result.append(Service("ui",8501,[sys.executable,"-m","streamlit","run",str(ROOT / "ui/app.py"),
        "--server.address","127.0.0.1","--server.port","8501","--server.headless","true",
        "--browser.gatherUsageStats","false"],"/_stcore/health"))
    return result


def redact(text: str, env: dict[str, str]) -> str:
    # 短值不可能是密钥：形如 CODEBUDDY_API_KEY_HELPER_DISABLED=1 的开关变量
    # 若参与替换会把 JSON 里的数字（如 "version": 1）误脱敏为 [REDACTED]，
    # 污染持久化数据（result_json）导致 json.loads 失败。
    values = {v for k, v in env.items() if v and len(v) >= 8 and any(
        word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", text)


def check_port(port: int) -> None:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise StartupError(f"端口 {port} 已被占用或无法监听；请先停止已有服务。") from exc


def preflight(specs: list[Service], env: dict[str, str], offline: bool) -> None:
    missing = [name for name in ("uvicorn", "fastapi", "httpx", "psutil", "playwright", "streamlit")
               if importlib.util.find_spec(name) is None]
    if missing:
        raise StartupError("缺少依赖：" + ", ".join(missing) +
                           "；请用当前解释器执行 -m pip install -r requirements.txt。")
    if not offline:
        key = env.get("INTERN_API_KEY", "").strip()
        if not key or key in {"在这里填端砚key", "你的端砚API密钥", "端砚key"}:
            raise StartupError("请在项目根目录 .env 中填写 INTERN_API_KEY，再重新启动。")
        try:
            entries = distribution("litellm").entry_points
        except PackageNotFoundError as exc:
            raise StartupError("缺少 LiteLLM 代理；请安装 requirements.txt 中的依赖。") from exc
        if not any(e.name == "litellm" and e.group == "console_scripts" for e in entries):
            raise StartupError("当前 LiteLLM 安装缺少代理启动入口。")
        from app.runner import resolve_codex_bin
        try:
            resolve_codex_bin(str(ROOT))
        except OSError as exc:
            raise StartupError(str(exc)) from exc
    portal_url = env.get("PROCUREMENT_PORTAL_URL", "http://127.0.0.1:8200").rstrip("/")
    if portal_url != "http://127.0.0.1:8200":
        raise StartupError("统一入口管理本地演示后台，PROCUREMENT_PORTAL_URL 必须为 http://127.0.0.1:8200。")
    for spec in specs:
        check_port(spec.port)


def is_healthy(spec: Service, env: dict[str, str]) -> bool:
    import httpx
    headers = {"Authorization": "Bearer " + env.get("INTERN_API_KEY", "")} if spec.name == "proxy" else {}
    try:
        response = httpx.get(f"http://127.0.0.1:{spec.port}{spec.health_path}",
                             headers=headers, timeout=0.8, trust_env=False)
        if response.status_code != 200:
            return False
        if spec.name == "ui":
            return response.text.strip() == "ok"
        payload = response.json()
        if spec.name == "proxy":
            return any(item.get("id") == "intern-s2" for item in payload.get("data", []))
        if spec.name == "api":
            return payload.get("service") == "restock-pilot" and payload.get("status") == "ok"
        return payload.get("status") == "ok"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return False


class Supervisor:
    def __init__(self, specs: list[Service], env: dict[str, str], log_dir: Path,
                 timeout: float = 90):
        self.specs, self.env, self.log_dir, self.timeout = specs, env, log_dir, timeout
        self.children = []
        self.stopping = threading.Event()

    def _log(self, pipe, path):
        # Raw child output is never forwarded to the terminal or written to disk.
        with path.open("a", encoding="utf-8") as output:
            for line in iter(pipe.readline, b""):
                output.write(redact(line.decode("utf-8", errors="replace"), self.env))
                output.flush()
        pipe.close()

    def start(self):
        import psutil
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for spec in self.specs:
            if self.stopping.is_set():
                raise StartupError("启动已取消。")
            # Recheck immediately before launch; never adopt or stop an existing server.
            check_port(spec.port)
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen(spec.command, cwd=ROOT, env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **options)
            try:
                owner = psutil.Process(proc.pid)
                owner.create_time()  # Cache identity so later PID reuse is detectable.
            except psutil.NoSuchProcess:
                proc.stdout.close()
                raise StartupError(f"{spec.name} 启动后立即退出。")
            reader = threading.Thread(target=self._log,
                args=(proc.stdout, self.log_dir / f"{spec.name}.log"), daemon=True)
            self.children.append((spec, proc, owner, reader))
            reader.start()
            deadline = time.monotonic() + self.timeout
            while not is_healthy(spec, self.env):
                self._check_children()
                if self.stopping.wait(0.15):
                    raise StartupError("启动已取消。")
                if time.monotonic() >= deadline:
                    raise StartupError(f"{spec.name} 启动超时，请查看脱敏日志。")
            self._check_children()
            print(f"已就绪：{spec.name} http://127.0.0.1:{spec.port}", flush=True)

    def _check_children(self):
        for spec, proc, _, _ in self.children:
            if proc.poll() is not None:
                raise StartupError(f"{spec.name} 已退出（退出码 {proc.returncode}），请查看脱敏日志。")

    def wait(self):
        while not self.stopping.wait(0.5):
            self._check_children()

    def stop(self):
        import psutil
        self.stopping.set()
        for _, proc, owner, reader in reversed(self.children):
            # Only this run's process identities and their descendants are eligible.
            try:
                if owner.is_running():
                    tree = owner.children(recursive=True) + [owner]
                    for child in reversed(tree):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    _, alive = psutil.wait_procs(tree, timeout=3)
                    for child in alive:
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    psutil.wait_procs(alive, timeout=3)
            except psutil.NoSuchProcess:
                pass
            finally:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                reader.join(timeout=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RestockPilot 统一启动；Ctrl+C 停止本次启动的服务。")
    parser.add_argument("action", nargs="?", choices=("start", "status"), default="start")
    parser.add_argument("--offline", action="store_true", help="关闭模型，仅启动 API 和演示后台")
    parser.add_argument("--check", action="store_true", help="只检查配置、依赖和端口，不启动")
    args = parser.parse_args(argv)
    try:
        from app.config import load_environment
        load_environment()
    except ImportError:
        print("请先用项目解释器执行 -m pip install -r requirements.txt。", file=sys.stderr)
        return 1
    env = os.environ.copy()
    mode = "off" if args.offline else env.get("RESTOCK_AGENT_MODE", "required").lower()
    if mode not in {"off", "required"}:
        print("RESTOCK_AGENT_MODE 只支持 required 或 off。", file=sys.stderr)
        return 1
    offline = mode == "off"
    env.update(RESTOCK_AGENT_MODE=mode, PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env["RESTOCK_APPROVAL_TOKEN"] = env.get("RESTOCK_APPROVAL_TOKEN") or secrets.token_urlsafe(32)
    specs = services(offline)
    supervisor = None
    previous_handlers = {}
    try:
        if args.action == "status":
            ready = [(spec, is_healthy(spec, env)) for spec in specs]
            for spec, ok in ready:
                print(f"{spec.name}: {'就绪' if ok else '未就绪'}")
            return 0 if all(ok for _, ok in ready) else 1
        preflight(specs, env, offline)
        if args.check:
            print("配置、依赖与端口检查通过。此检查不验证模型密钥有效性或端到端任务。")
            return 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        supervisor = Supervisor(specs, env, ROOT / "outputs" / "launcher" / stamp)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: supervisor.stopping.set())
        supervisor.start()
        print("服务已启动。模式：" + ("离线，未启用模型" if offline else "真实模型（密钥与推理尚未验收）"), flush=True)
        print("工作台：http://127.0.0.1:8501；采购演示：http://127.0.0.1:8200", flush=True)
        print(f"日志：{supervisor.log_dir}\n保持本窗口运行，按 Ctrl+C 统一停止。", flush=True)
        supervisor.wait()
        return 0
    except (StartupError, OSError, ImportError) as exc:
        print(redact(str(exc), env), file=sys.stderr)
        if supervisor:
            print(f"日志：{supervisor.log_dir}", file=sys.stderr)
        return 1
    finally:
        if supervisor:
            supervisor.stop()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
