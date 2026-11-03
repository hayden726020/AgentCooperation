import asyncio
import codecs
import inspect
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import psutil


logger = logging.getLogger(__name__)

LineCallback = Callable[[str, str], Any]


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


async def _read_stream_lines(
    stream: asyncio.StreamReader,
    sink: List[str],
    on_line: Optional[LineCallback],
    stream_name: str,
) -> None:
    """Drain one pipe by chunks, framing lines without any per-line length limit."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    flush_threshold = 4 * 1024 * 1024

    async def notify(text: str) -> None:
        if on_line is None:
            return
        try:
            outcome = on_line(clean_ansi(text).rstrip("\r\n"), stream_name)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.warning("on_line callback failed for %s", stream_name, exc_info=True)

    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        if "\n" in pending:
            parts = pending.split("\n")
            pending = parts.pop()
            for line in parts:
                sink.append(line + "\n")
                await notify(line)
        if len(pending) >= flush_threshold:
            # A single enormous line must not grow memory without bound; emit it
            # in pieces so the full text is still preserved in the sink.
            sink.append(pending)
            await notify(pending)
            pending = ""

    pending += decoder.decode(b"", final=True)
    if pending:
        sink.append(pending)
        await notify(pending)


async def _kill_and_drain(
    proc: asyncio.subprocess.Process,
    tasks: List[asyncio.Task],
    stdout_chunks: List[str],
    stderr_chunks: List[str],
) -> tuple[str, str]:
    """Kill the process tree, then wait for readers to hit EOF and return partial output."""
    errors = []
    try:
        await asyncio.wait_for(asyncio.to_thread(kill_process_tree, proc.pid), timeout=4)
    except Exception as exc:
        errors.append(str(exc))
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(
            asyncio.shield(asyncio.gather(*tasks, return_exceptions=True)),
            timeout=3,
        )
    except Exception as exc:
        errors.append(str(exc))
    if errors:
        raise RuntimeError(f"Process cleanup and pipe drain failed: {'; '.join(errors)}")
    return "".join(stdout_chunks), "".join(stderr_chunks)


async def _run_streaming_command(
    proc: asyncio.subprocess.Process,
    on_line: LineCallback,
    timeout: float,
    input_text: Optional[str],
) -> tuple[str, str, int, bool]:
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    reader_tasks = [
        asyncio.create_task(_read_stream_lines(proc.stdout, stdout_chunks, on_line, "stdout")),
        asyncio.create_task(_read_stream_lines(proc.stderr, stderr_chunks, on_line, "stderr")),
    ]
    wait_task = asyncio.create_task(proc.wait())

    async def write_input() -> None:
        if input_text is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            await proc.stdin.drain()
        except OSError:
            # The child may exit or be killed while stdin is still being written.
            pass
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    write_task = asyncio.create_task(write_input()) if input_text is not None else None
    all_tasks = [*reader_tasks, wait_task]
    if write_task is not None:
        all_tasks.append(write_task)

    timed_out = False
    try:
        await asyncio.wait(all_tasks, timeout=timeout)
        if any(not task.done() for task in all_tasks):
            timed_out = True
            await _kill_and_drain(proc, all_tasks, stdout_chunks, stderr_chunks)
        for task in all_tasks:
            task.result()
    except asyncio.CancelledError:
        if any(not task.done() for task in all_tasks):
            cleanup_task = asyncio.create_task(
                _kill_and_drain(proc, all_tasks, stdout_chunks, stderr_chunks)
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
        raise
    except Exception:
        if any(not task.done() for task in all_tasks):
            await _kill_and_drain(proc, all_tasks, stdout_chunks, stderr_chunks)
        raise

    exit_code = proc.returncode if proc.returncode is not None else -1
    return "".join(stdout_chunks), "".join(stderr_chunks), exit_code, timed_out


async def run_async_command(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 300.0,
    input_text: Optional[str] = None,
    on_line: Optional[LineCallback] = None
) -> SubprocResult:
    """
    异步安全执行本地 CLI 子进程：
    1. 自动注入 CI/非交互环境变量，防止 CLI 工具（OpenCode, Gemini 等）等待用户交互卡死
    2. 超时看门狗自动清理进程树
    3. 自动清洗 ANSI 转义字符
    4. 可选 on_line 回调逐行推送 stdout/stderr，用于运行台实时展示
    """
    start_time = time.time()

    # 构造非交互式环境变量，剥离敏感凭据
    sensitive_substrings = ("KEY", "SECRET", "TOKEN", "AUTH", "PASSWORD", "PASS", "CREDENTIAL", "AWS", "GITHUB", "OPENAI", "ZHIPU", "DEEPSEEK")
    merged_env = {
        key: value for key, value in os.environ.items()
        if key.upper() in SAFE_ENV_KEYS and not any(sub in key.upper() for sub in sensitive_substrings)
    }
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
        stderr=asyncio.subprocess.PIPE,
    )

    if on_line is not None:
        raw_stdout, raw_stderr, exit_code, timed_out = await _run_streaming_command(
            proc, on_line, timeout, input_text
        )
    else:
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

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = -1 if timed_out else (proc.returncode or 0)

    duration = time.time() - start_time
    return SubprocResult(
        command=cmd,
        exit_code=exit_code,
        stdout=clean_ansi(raw_stdout),
        stderr=clean_ansi(raw_stderr),
        duration_s=duration,
        timed_out=timed_out
    )
