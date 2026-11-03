import ctypes
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from execution.subproc import LineCallback, run_async_command


@dataclass
class TestResult:
    passed: bool
    total_tests: int
    failed_tests: int
    summary: str
    raw_output: str
    duration_s: float


def split_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    shell = ctypes.windll.shell32
    kernel = ctypes.windll.kernel32
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise ValueError("Cannot parse test command")
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


class TestRunner:
    """Execute selected target tests with a credential-free subprocess environment."""

    __test__ = False

    def __init__(self, worktree_path: Path, custom_test_cmd: Optional[str] = None,
                 *, test_paths: Optional[list[str]] = None,
                 custom_test_argv: Optional[list[str]] = None):
        self.worktree_path = Path(worktree_path).resolve()
        self.custom_test_cmd = custom_test_cmd
        self.test_paths = test_paths
        self.custom_test_argv = custom_test_argv

    def detect_command(self) -> list[str]:
        if self.custom_test_argv is not None:
            if not self.custom_test_argv or not all(isinstance(arg, str) for arg in self.custom_test_argv):
                raise ValueError("Test argv must be a nonempty list of strings")
            return list(self.custom_test_argv)
        if self.custom_test_cmd:
            # Strictly validate custom test command
            forbidden_tokens = [";", "&", "|", "`", "$", "\n", "\r", ">", "<"]
            if any(t in self.custom_test_cmd for t in forbidden_tokens):
                raise ValueError(f"Custom test command contains disallowed shell operators: {self.custom_test_cmd}")
            command = split_command(self.custom_test_cmd)
            if not command:
                raise ValueError("Test command cannot be empty")
            allowed_runners = ("pytest", "pytest.exe", "python", "python.exe")
            runner_name = Path(command[0]).name.lower()
            if runner_name not in allowed_runners:
                raise ValueError(f"Custom test command runner not permitted: {command[0]}")
            # Ensure no rogue options like -p, --rootdir, --confcutdir are passed
            for arg in command[1:]:
                if arg.startswith("-p") or arg.startswith("--rootdir") or arg.startswith("--confcutdir"):
                    raise ValueError(f"Custom test command contains disallowed plugin/rootdir flag: {arg}")
            return command

        if self.test_paths is not None:
            paths = list(self.test_paths)
        else:
            paths = sorted(p.name for p in self.worktree_path.glob("test_*.py"))
            paths += sorted(p.name for p in self.worktree_path.glob("*_test.py"))
            if not paths and (self.worktree_path / "package.json").is_file():
                return ["cmd", "/d", "/c", "npm", "test"] if os.name == "nt" else ["npm", "test"]
            if (self.worktree_path / "tests").is_dir():
                paths.append("tests")
        if not paths:
            raise ValueError("No target test paths were provided or discovered")

        validated_paths = []
        for item in paths:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("Invalid target test path: empty or non-string")
            if item.startswith("-"):
                raise ValueError(f"Invalid target test path: cannot be a flag ({item})")
            if any(char in item for char in (";", "|", "&", "$", "`", "\n", "\r", "<", ">", "\x00")):
                raise ValueError(f"Invalid target test path contains shell metacharacters: {item}")
            p_obj = Path(item)
            if p_obj.is_absolute():
                raise ValueError(f"Invalid target test path: cannot be absolute ({item})")
            if ".." in p_obj.parts:
                raise ValueError(f"Invalid target test path: cannot traverse outside worktree ({item})")
            full_path = (self.worktree_path / p_obj).resolve()
            if not full_path.is_relative_to(self.worktree_path):
                raise ValueError(f"Invalid target test path: escaped worktree ({item})")
            if not full_path.exists():
                raise ValueError(f"Invalid target test path: does not exist ({item})")
            if full_path.is_symlink() or (hasattr(full_path, "is_junction") and full_path.is_junction()):
                raise ValueError(f"Invalid target test path: cannot be a symlink or junction ({item})")
            validated_paths.append(str(full_path.relative_to(self.worktree_path).as_posix()))

        return [sys.executable, "-m", "pytest", "-s", "-p", "no:cacheprovider", "-v", "--tb=short",
                f"--rootdir={self.worktree_path}", f"--confcutdir={self.worktree_path}", *validated_paths]

    def build_env(self, worktree_path: Optional[Path] = None) -> dict[str, str]:
        wt = Path(worktree_path or self.worktree_path).resolve()
        sandbox_tmp = (wt / ".test_sandbox").resolve()
        sandbox_tmp.mkdir(parents=True, exist_ok=True)
        sensitive_substrings = ("KEY", "SECRET", "TOKEN", "AUTH", "PASSWORD", "PASS", "CREDENTIAL", "AWS", "GITHUB", "OPENAI", "ZHIPU", "DEEPSEEK")
        from execution.subproc import SAFE_ENV_KEYS
        env = {
            k: v for k, v in os.environ.items()
            if k.upper() in SAFE_ENV_KEYS and not any(sub in k.upper() for sub in sensitive_substrings)
        }
        env.update({
            "HOME": str(sandbox_tmp),
            "USERPROFILE": str(sandbox_tmp),
            "TEMP": str(sandbox_tmp),
            "TMP": str(sandbox_tmp),
            "TMPDIR": str(sandbox_tmp),
            "LOCALAPPDATA": str(sandbox_tmp),
            "APPDATA": str(sandbox_tmp),
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        })
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(proxy_var, None)
        return env

    async def run(self, timeout: float = 120.0, on_line: Optional[LineCallback] = None) -> TestResult:
        try:
            cmd = self.detect_command()
        except ValueError as exc:
            return TestResult(False, 0, 0, str(exc), str(exc), 0)

        hardened_env = self.build_env(self.worktree_path)
        res = await run_async_command(
            cmd=cmd,
            cwd=self.worktree_path,
            env=hardened_env,
            timeout=timeout,
            on_line=on_line,
        )
        output = f"{res.stdout}\n{res.stderr}".strip()
        counts = {}
        for line in reversed(output.splitlines()):
            matches = re.findall(r"\b(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected)\b", line)
            if matches:
                counts = {label.rstrip("s") if label.startswith("error") else label: int(count)
                          for count, label in matches}
                break
        total = sum(count for label, count in counts.items() if label != "deselected")
        failed = counts.get("failed", 0) + counts.get("error", 0)
        successful = counts.get("passed", 0)
        unittest_total = re.search(r"^Ran (\d+) tests?\b", output, flags=re.MULTILINE)
        if unittest_total:
            total = int(unittest_total.group(1))
            unittest_failures = re.search(r"^FAILED \(([^\n]*)\)", output, flags=re.MULTILINE)
            if unittest_failures:
                failed = sum(int(number) for number in re.findall(r"(?:^|,\s*)(?:failures|errors|unexpected successes)=(\d+)", unittest_failures.group(1)))
            skipped = re.search(r"\bskipped=(\d+)\b", output)
            expected_failures = re.search(r"\bexpected failures=(\d+)\b", output)
            successful = max(0, total - failed - (int(skipped.group(1)) if skipped else 0)
                             - (int(expected_failures.group(1)) if expected_failures else 0))
        mocha_counts = {label: int(number) for number, label in re.findall(
            r"^\s*(\d+)\s+(passing|failing|pending)\b", output, flags=re.MULTILINE
        )}
        if mocha_counts:
            total = sum(mocha_counts.values())
            failed = mocha_counts.get("failing", 0)
            successful = mocha_counts.get("passing", 0)
        tap_counts = {label: int(number) for label, number in re.findall(
            r"^\s*(?:#|ℹ)\s*(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\b",
            output, flags=re.MULTILINE
        )}
        if tap_counts:
            total = tap_counts.get("tests", sum(count for label, count in tap_counts.items() if label != "tests"))
            failed = tap_counts.get("fail", 0) + tap_counts.get("cancelled", 0)
            successful = tap_counts.get("pass", 0)
        zero_tests = bool(re.search(r"^(?:no tests (?:ran|found)|Ran 0 tests?\b|Tests:\s*0 total)", output, flags=re.MULTILINE | re.IGNORECASE))
        counted_runner = bool(counts or unittest_total or mocha_counts or tap_counts)
        passed = res.is_success and failed == 0 and not zero_tests and successful > 0
        summary = "All tests passed successfully." if passed else "Tests failed or encountered errors."
        if not counted_runner and not zero_tests and not res.timed_out:
            summary = "Unsupported test output; no successful target tests were verified."
        elif (zero_tests or successful == 0) and not res.timed_out:
            summary = "No target tests passed; validation did not pass."
        if res.timed_out:
            summary = f"Tests timed out after {timeout}s."
        return TestResult(passed, total, failed, summary, output, res.duration_s)
