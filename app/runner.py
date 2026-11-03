# -*- coding: utf-8 -*-
"""runner.py：codex exec 子进程封装。

职责：启动 codex exec（--json JSONL 事件流）、读取事件、超时终止（Windows
使用 Job 管理本次进程及后代）、结构化返回结果。阶段上下文（上一阶段产物摘要）
由调用方注入 prompt，不依赖进程内存。
"""

import json
from app.processes import ProcessScope
import os
import shutil
import subprocess
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

    prompt 经匿名管道传给 `codex exec -`，避免 Windows argv 长度限制；
    Node/npm 包装入口不能可靠继承磁盘文件形式的 stdin。写入、stdout、
    stderr 分线程处理，全部受同一超时与进程归属控制。
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

    events: List[CodexEvent] = []
    stderr = ""
    timed_out = False
    stderr_tail = bytearray()
    stream_error = []
    scope = None

    def stop_owned():
        try:
            scope.stop()
        except OSError as exc:
            stream_error.append(f"CLI process cleanup failed: {exc}")
            # Closing a Windows Job remains a fallback kill-on-close action.
            try:
                scope.close()
            except OSError as close_exc:
                stream_error.append(f"CLI job close failed: {close_exc}")

    def _kill_tree():
        nonlocal timed_out
        timed_out = True
        stop_owned()

    timer = threading.Timer(timeout_s, _kill_tree)
    try:
        try:
            scope = ProcessScope()
            proc = scope.spawn(cmd, cwd=workdir, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            pending = bytearray()
            max_line = 1024 * 1024  # Includes the newline when present.
            def accept(line):
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(CodexEvent(value))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    if pending:
                        accept(pending)
                    break
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    stream_error.append('CLI event stream exceeded 8 MB')
                    stop_owned(); break
                pending.extend(chunk)
                while pending:
                    end = pending.find(b'\n')
                    if end == -1:
                        if len(pending) > max_line:
                            stream_error.append('CLI event line exceeded 1 MB')
                            stop_owned()
                            return
                        break
                    if end + 1 > max_line:
                        stream_error.append('CLI event line exceeded 1 MB')
                        stop_owned()
                        return
                    accept(pending[:end+1])
                    del pending[:end+1]

        def write_prompt():
            try:
                proc.stdin.write(prompt.encode('utf-8'))
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # The exit code/timeout below records an early CLI failure.
            finally:
                proc.stdin.close()

        readers = [threading.Thread(target=write_prompt, daemon=True),
                   threading.Thread(target=read_errors, daemon=True),
                   threading.Thread(target=read_events, daemon=True)]
        for reader in readers:
            reader.start()
        try:
            proc.wait()
        except BaseException:
            stop_owned()
            raise
        finally:
            # The Job/session still owns descendants after the root exits.
            stop_owned()
            for reader in readers:
                reader.join(timeout=5)
            proc.stdout.close(); proc.stderr.close()
        stderr = _decode_output(bytes(stderr_tail))
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join(timeout=6)
        if scope is not None:
            try:
                scope.close()
            except OSError as exc:
                stream_error.append(f"CLI job close failed: {exc}")

    if stream_error:
        stderr += '\n' + '\n'.join(stream_error)
    return CodexRunResult(
        exit_code=1 if stream_error or timed_out else proc.returncode, events=events, stderr=stderr,
        duration_s=round(time.time() - start, 1), timed_out=timed_out,
    )
