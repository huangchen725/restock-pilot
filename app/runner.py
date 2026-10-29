# -*- coding: utf-8 -*-
"""runner.py：codex exec 子进程封装。

职责：启动 codex exec（--json JSONL 事件流）、读取事件、超时终止（Windows
杀整棵进程树 taskkill /T）、结构化返回结果。阶段上下文（上一阶段产物摘要）
由调用方注入 prompt，不依赖进程内存。
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

CODEX_BIN = (
    r"C:\Users\LENOVO\.workbuddy\binaries\node\workspace"
    r"\node_modules\.bin\codex.cmd"
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
    def tool_calls(self) -> List[str]:
        return [e.tool_command for e in self.events if e.is_tool_exec]

    @property
    def final_text(self) -> str:
        for e in reversed(self.events):
            if e.final_message:
                return e.final_message
        return ""


def run_codex_task(prompt: str, workdir: str, timeout_s: int = 600,
                   codex_home: Optional[str] = None) -> CodexRunResult:
    """同步执行一次 codex exec，返回结构化结果。

    prompt 经临时文件 + stdin 传入（`codex exec -`）：Windows cmd.exe 对
    .cmd 批处理的命令行有 8191 字符上限，长 prompt 走 argv 会报
    "命令行太长" 直接 exit=1。
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home or str(
        Path(__file__).resolve().parents[1] / "agent-integration"
    )
    cmd = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check", "-"]
    start = time.time()

    prompt_handle = tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="codex-prompt-", delete=False, encoding="utf-8",
    )
    prompt_handle.write(prompt)
    prompt_handle.close()
    events: List[CodexEvent] = []
    stderr = ""
    timed_out = False

    def _kill_tree():
        nonlocal timed_out
        timed_out = True
        # Windows：杀整棵进程树（codex 可能派生 powershell 子进程）
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )

    timer = threading.Timer(timeout_s, _kill_tree)
    timer.start()
    try:
        with open(prompt_handle.name, "rb") as stdin_fh:
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=env,
                stdin=stdin_fh,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    events.append(CodexEvent(json.loads(line)))
                except json.JSONDecodeError:
                    continue  # 非事件行（横幅等）跳过
            stderr = _decode_output(proc.stderr.read() if proc.stderr else b"")
            proc.wait()
    finally:
        timer.cancel()
        try:
            os.unlink(prompt_handle.name)
        except OSError:
            pass

    return CodexRunResult(
        exit_code=proc.returncode, events=events, stderr=stderr,
        duration_s=round(time.time() - start, 1), timed_out=timed_out,
    )
