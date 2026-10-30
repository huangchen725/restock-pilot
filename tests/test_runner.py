"""Portable executable discovery and subprocess launch failures (no model calls)."""

import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import runner


def test_path_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("RESTOCK_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    # A command alias stands in for codex; exercise real PATH lookup.
    real_which = shutil.which
    monkeypatch.setattr(runner.shutil, "which", lambda name: real_which(
        Path(sys.executable).name if name == "codex" else name))
    assert Path(runner.resolve_codex_bin(str(tmp_path))) == Path(sys.executable).resolve()


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_path_with_spaces(monkeypatch, tmp_path, relative):
    executable = tmp_path / "custom tools" / ("codex.cmd" if os.name == "nt" else "codex")
    executable.parent.mkdir()
    executable.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    executable.chmod(0o755)
    value = str(executable.relative_to(tmp_path)) if relative else str(executable)
    monkeypatch.setenv("RESTOCK_CODEX_BIN", value)
    assert Path(runner.resolve_codex_bin(str(tmp_path))) == executable.resolve()


def test_bad_explicit_config_does_not_fall_back(monkeypatch, tmp_path):
    monkeypatch.setenv("RESTOCK_CODEX_BIN", "./missing/codex")
    monkeypatch.setattr(runner.shutil, "which", lambda name: sys.executable if name == "codex" else None)
    result = runner.run_codex_task("hello", str(tmp_path))
    assert result.exit_code == 127
    assert "RESTOCK_CODEX_BIN" in result.stderr
    assert not result.timed_out


def test_missing_path_has_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("RESTOCK_CODEX_BIN", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    result = runner.run_codex_task("hello", str(tmp_path))
    assert result.exit_code == 127
    assert "PATH" in result.stderr
    assert "RESTOCK_CODEX_BIN" in result.stderr


def test_missing_cli_becomes_agent_stage_error(monkeypatch):
    from app import main
    monkeypatch.setenv("RESTOCK_AGENT_MODE", "required")
    monkeypatch.delenv("RESTOCK_CODEX_BIN", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(main.AgentStageError, match="Codex CLI was not found"):
        main._run_agent_stage("planner", "context", "request")


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_real_windows_cmd_launcher_in_path_with_spaces(monkeypatch, tmp_path):
    executable = tmp_path / "tools with spaces" / "codex.cmd"
    executable.parent.mkdir()
    event = json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "local stub completed"}})
    executable.write_text("@echo off\necho " + event + "\n", encoding="ascii")
    monkeypatch.setenv("RESTOCK_CODEX_BIN", str(executable))
    result = runner.run_codex_task("补货任务" * 3000, str(tmp_path), timeout_s=10)
    assert result.exit_code == 0, result.stderr
    assert result.final_text == "local stub completed"


def test_launch_failure_cleans_prompt_without_starting_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "resolve_codex_bin", lambda _: sys.executable)
    prompt_paths = []

    def fail_launch(*args, **kwargs):
        prompt_paths.append(Path(kwargs["stdin"].name))
        raise PermissionError("launch denied")

    monkeypatch.setattr(runner.subprocess, "Popen", fail_launch)
    monkeypatch.setattr(runner.threading.Timer, "start", lambda _: pytest.fail("timer started before launch"))
    result = runner.run_codex_task("hello", str(tmp_path))
    assert result.exit_code == 127
    assert "launch denied" in result.stderr
    assert prompt_paths and not prompt_paths[0].exists()


def test_long_unicode_prompt_uses_stdin_and_isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "resolve_codex_bin", lambda _: sys.executable)
    monkeypatch.setenv("CODEX_HOME", "personal-home")
    monkeypatch.setenv("RESTOCK_APPROVAL_TOKEN", "human-only-secret")
    prompt = "补货预算调整\n" * 2000
    observed = {}

    class Process:
        pid = 123
        returncode = 0
        stdout = io.BytesIO((json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "done"}}) + "\n").encode())
        stderr = io.BytesIO()

        def wait(self):
            return 0

    def launch(cmd, **kwargs):
        assert "RESTOCK_APPROVAL_TOKEN" not in kwargs["env"]
        observed.update(cmd=cmd, prompt=kwargs["stdin"].read().decode("utf-8"),
                        home=kwargs["env"]["CODEX_HOME"], path=Path(kwargs["stdin"].name))
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    result = runner.run_codex_task(prompt, str(tmp_path), codex_home=str(tmp_path / "agent config"))
    assert result.final_text == "done"
    assert observed["prompt"] == prompt
    assert observed["cmd"] == [sys.executable, "exec", "--json", "--skip-git-repo-check", "-"]
    assert observed["home"] == str(tmp_path / "agent config")
    assert os.environ["CODEX_HOME"] == "personal-home"
    assert not observed["path"].exists()
