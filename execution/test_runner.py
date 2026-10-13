import asyncio
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from execution.subproc import run_async_command, SubprocResult


@dataclass
class TestResult:
    passed: bool
    total_tests: int
    failed_tests: int
    summary: str
    raw_output: str
    duration_s: float


class TestRunner:
    """
    本地自动化测试调度器：
    1. 自动探查项目技术栈（pytest / npm test / 自定义测试命令）
    2. 执行测试并结构化解析成功/失败报告
    3. 输出失败堆栈供 Primary Agent 开展定向自修
    """

    def __init__(self, worktree_path: Path, custom_test_cmd: Optional[str] = None):
        self.worktree_path = worktree_path
        self.custom_test_cmd = custom_test_cmd

    def detect_command(self) -> list[str]:
        if self.custom_test_cmd:
            return shlex.split(self.custom_test_cmd, posix=(os.name != "nt"))


        # 检查 Python 测试
        if (
            (self.worktree_path / "pytest.ini").exists()
            or (self.worktree_path / "tests").exists()
            or list(self.worktree_path.glob("test_*.py"))
        ):
            return ["pytest", "-v", "--tb=short"]

        # 检查 Node.js 测试
        pkg_json = self.worktree_path / "package.json"
        if pkg_json.exists():
            return ["npm", "test"]

        # 默认：如果目录下有任何 python 脚本，尝试用 pytest
        return ["pytest", "-v", "--tb=short"]

    async def run(self, timeout: float = 120.0) -> TestResult:
        cmd = self.detect_command()
        res: SubprocResult = await run_async_command(
            cmd=cmd,
            cwd=self.worktree_path,
            timeout=timeout
        )

        output = f"{res.stdout}\n{res.stderr}".strip()
        passed = (res.exit_code == 0) and not res.timed_out

        # 简单提取统计信息
        failed_count = 0
        total_count = 1

        if "passed" in output or "failed" in output:
            # 常见 pytest 模式
            if "failed" in output:
                failed_count = 1
            if passed:
                failed_count = 0

        summary = "All tests passed successfully." if passed else "Tests failed or encountered errors."
        if res.timed_out:
            summary = f"Tests timed out after {timeout}s."

        return TestResult(
            passed=passed,
            total_tests=total_count,
            failed_tests=failed_count,
            summary=summary,
            raw_output=output,
            duration_s=res.duration_s
        )
