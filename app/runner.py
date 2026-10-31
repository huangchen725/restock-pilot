# -*- coding: utf-8 -*-
"""runner.py：codex exec 子进程封装。

职责：启动 codex exec（--json JSONL 事件流）、读取事件、超时终止（Windows
杀整棵进程树 taskkill /T）、结构化返回结果。阶段上下文（上一阶段产物摘要）
由调用方注入 prompt，不依赖进程内存。
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional


def child_environment(extra=None):
    # Allow only OS bootstrap variables; never inherit .env, DB paths, approval
    # credentials, personal model credentials, proxies or Python injection knobs.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOME', 'LANG', 'SYSTEMDRIVE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    env.update(extra or {})
    return env

def resolve_codex_bin(workdir: str) -> str:
    """显式配置优先，否则查 PATH；相对路径以任务工作目录为基准。"""
    configured = os.getenv("RESTOCK_CODEX_BIN", "").strip()
    candidate = os.path.expandvars(os.path.expanduser(configured or "codex"))
    if os.path.isabs(candidate) or "/" in candidate or "\\" in candidate:
        path = Path(candidate)
        if not path.is_absolute():
            path = Path(workdir) / path
        candidate = str(path.resolve())
    resolved = shutil.which(candidate)
    if resolved:
        return str(Path(resolved).resolve())
    if configured:
        raise FileNotFoundError(
            "RESTOCK_CODEX_BIN does not resolve to an executable. "
            "Set it to a program path (without command arguments), "
            "or unset it to find codex on PATH."
        )
    raise FileNotFoundError(
        "Codex CLI was not found on PATH. Install it and restart the service, "
        "or set RESTOCK_CODEX_BIN to its executable path."
    )


def _decode_output(data: bytes) -> str:
    """codex 的 stderr 在 Windows 上可能是 GBK（本地化报错），utf-8 优先、gbk 兜底。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class CodexEvent:
    def __init__(self, raw: dict):
        self.type = raw.get("type")
        self.raw = raw

    @property
    def is_tool_exec(self) -> bool:
        if self.type != "item.completed":
            return False
        return (self.raw.get("item") or {}).get("type") == "command_execution"

    @property
    def tool_command(self) -> Optional[str]:
        if self.is_tool_exec:
            return (self.raw.get("item") or {}).get("command")
        return None

    @property
    def final_message(self) -> Optional[str]:
        if self.type != "item.completed":
            return None
        item = self.raw.get("item") or {}
        if item.get("type") == "agent_message":
            return item.get("text")
        return None


class CodexRunResult:
    def __init__(self, exit_code: int, events: List[CodexEvent],
                 stderr: str, duration_s: float, timed_out: bool = False):
        self.exit_code = exit_code
        self.events = events
        self.stderr = stderr
        self.duration_s = duration_s
        self.timed_out = timed_out

    @property
    def tool_calls(self) -> list[dict | str]:
        result = []
        for event in self.events:
            item = event.raw.get('item') or {}
            if event.type == 'item.completed' and item.get('type') == 'mcp_tool_call':
                result.append({k: item.get(k) for k in ('server', 'tool', 'arguments', 'result', 'error', 'status')})
            elif event.is_tool_exec:
                result.append(event.tool_command)
        return result

    @property
    def final_text(self) -> str:
        for e in reversed(self.events):
            if e.final_message:
                return e.final_message
        return ""


def run_codex_task(prompt: str, workdir: str, timeout_s: int = 600,
                   codex_home: Optional[str] = None, child_env=None,
                   executable: Optional[str] = None) -> CodexRunResult:
    """同步执行一次 codex exec，返回结构化结果。

    prompt 经临时文件 + stdin 传入（`codex exec -`）：Windows cmd.exe 对
    .cmd 批处理的命令行有 8191 字符上限，长 prompt 走 argv 会报
    "命令行太长" 直接 exit=1。
    """
    env = child_environment(child_env)
    env["CODEX_HOME"] = codex_home or str(
        Path(__file__).resolve().parents[1] / "agent-integration"
    )
    start = time.time()
    try:
        executable = executable or resolve_codex_bin(workdir)
    except OSError as exc:
        return CodexRunResult(127, [], str(exc), round(time.time() - start, 1))
    cmd = [executable, "exec", "--json", "--skip-git-repo-check", "-"]

    prompt_handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="codex-prompt-", delete=False, encoding="utf-8",
        newline="",
    )
    prompt_handle.write(prompt)
    prompt_handle.close()
    events: List[CodexEvent] = []
    stderr = ""
    timed_out = False
    stderr_tail = bytearray()
    stream_error = []

    def _kill_tree():
        nonlocal timed_out
        timed_out = True
        _stop_owned_process(proc)

    timer = threading.Timer(timeout_s, _kill_tree)
    try:
        with open(prompt_handle.name, "rb") as stdin_fh:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=workdir, env=env,
                    stdin=stdin_fh,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == 'nt' else {'start_new_session': True}),
                )
            except OSError as exc:
                return CodexRunResult(
                    127, [], f"Unable to start Codex CLI: {exc}",
                    round(time.time() - start, 1),
                )
            # 启动成功后才启用超时，避免启动失败时访问尚不存在的 proc。
            timer.start()
            def read_errors():
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-65536]

            def read_events():
                total = 0
                for raw_line in proc.stdout:
                    total += len(raw_line)
                    if total > 8 * 1024 * 1024:
                        stream_error.append('CLI event stream exceeded 8 MB')
                        _stop_owned_process(proc)
                        break
                    try:
                        events.append(CodexEvent(json.loads(raw_line)))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

            readers = [threading.Thread(target=read_errors, daemon=True),
                       threading.Thread(target=read_events, daemon=True)]
            for reader in readers:
                reader.start()
            try:
                proc.wait()
            except BaseException:
                _stop_owned_process(proc)
                raise
            finally:
                for reader in readers:
                    reader.join(timeout=5)
                proc.stdout.close(); proc.stderr.close()
            stderr = _decode_output(bytes(stderr_tail))
            if stream_error:
                stderr += '\n' + stream_error[0]
    finally:
        timer.cancel()
        try:
            os.unlink(prompt_handle.name)
        except OSError:
            pass

    return CodexRunResult(
        exit_code=proc.returncode or (1 if stream_error else 0), events=events, stderr=stderr,
        duration_s=round(time.time() - start, 1), timed_out=timed_out,
    )


def _stop_owned_process(proc):
    """Terminate only the child started by this call and its descendants."""
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
