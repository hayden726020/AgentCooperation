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


SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "USER", "USERNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV", "CONDA_PREFIX", "NUMBER_OF_PROCESSORS",
}


def kill_process_tree(pid: int):
    """Kill and reap a process tree; report denied access or surviving processes."""
    try:
        parent = psutil.Process(pid)
        processes = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    errors = []
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            errors.append(str(exc))
    _, alive = psutil.wait_procs(processes, timeout=2)
    if errors or alive:
        raise RuntimeError(f"Process cleanup failed: {errors}; surviving PIDs: {[p.pid for p in alive]}")


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
    merged_env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
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

    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    timed_out = False

    communication = asyncio.create_task(proc.communicate(input=input_bytes))

    async def cleanup():
        try:
            await asyncio.wait_for(asyncio.to_thread(kill_process_tree, proc.pid), timeout=4)
        except Exception as cleanup_error:
            # Close the direct child and its pipes even when process-tree cleanup failed.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=3)
            except Exception as drain_error:
                raise RuntimeError(f"Process cleanup and pipe drain failed: {drain_error}") from cleanup_error
            raise
        return await asyncio.wait_for(asyncio.shield(communication), timeout=3)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            asyncio.shield(communication),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        stdout_bytes, stderr_bytes = await cleanup()
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
        raise

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
