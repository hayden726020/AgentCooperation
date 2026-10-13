import ctypes
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from execution.subproc import run_async_command


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
            command = split_command(self.custom_test_cmd)
            if not command:
                raise ValueError("Test command cannot be empty")
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
        for item in paths:
            path = (self.worktree_path / item).resolve()
            if (not item or item.startswith("-") or Path(item).is_absolute()
                    or not path.is_relative_to(self.worktree_path) or not path.exists()):
                raise ValueError(f"Invalid target test path: {item}")
        return [sys.executable, "-m", "pytest", "-v", "--tb=short",
                f"--rootdir={self.worktree_path}", f"--confcutdir={self.worktree_path}", *paths]

    async def run(self, timeout: float = 120.0) -> TestResult:
        try:
            cmd = self.detect_command()
        except ValueError as exc:
            return TestResult(False, 0, 0, str(exc), str(exc), 0)
        res = await run_async_command(cmd=cmd, cwd=self.worktree_path, timeout=timeout)
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
