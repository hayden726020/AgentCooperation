import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
import psutil


# 正则清洗 ANSI 转义字符序列（终端颜色、光标定位等），防止污染 LLM 上下文
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def clean_ansi(text: str) -> str:
    if not text:
        return ""
    return ANSI_ESCAPE_RE.sub("", text)


@dataclass
class SubprocResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def is_success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def kill_process_tree(pid: int):
    """安全终止进程及其所有子进程树"""
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


async def run_async_command(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 300.0,
    input_text: Optional[str] = None
) -> SubprocResult:
    """
    异步安全执行本地 CLI 子进程：
    1. 自动注入 CI/非交互环境变量，防止 CLI 工具（OpenCode, Gemini 等）等待用户交互卡死
    2. 超时看门狗自动清理进程树
    3. 自动清洗 ANSI 转义字符
    """
    start_time = time.time()
    
    # 构造非交互式环境变量
    merged_env = os.environ.copy()
    merged_env.update({
        "CI": "1",
        "NONINTERACTIVE": "1",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1"
    })
    if env:
        merged_env.update(env)

    cwd_str = str(cwd) if cwd else None
    stdin_pipe = asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd_str,
        env=merged_env,
        stdin=stdin_pipe,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    input_bytes = input_text.encode("utf-8") if input_text else None
    timed_out = False

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        kill_process_tree(proc.pid)
        try:
            stdout_bytes, stderr_bytes = await proc.communicate()
        except Exception:
            stdout_bytes, stderr_bytes = b"", b""

    duration = time.time() - start_time
    raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
    raw_stderr = stderr_bytes.decode("utf-8", errors="replace")

    return SubprocResult(
        command=cmd,
        exit_code=-1 if timed_out else (proc.returncode or 0),
        stdout=clean_ansi(raw_stdout),
        stderr=clean_ansi(raw_stderr),
        duration_s=duration,
        timed_out=timed_out
    )
