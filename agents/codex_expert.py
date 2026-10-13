import os
import re
import time
from pathlib import Path
from typing import Any
from agents.base import BaseAgent, AgentResult, parse_review_output, task_directory
from agents.gemini_reviewer import REVIEW_SCHEMA_PROMPT
from execution.subproc import run_async_command, SubprocResult
from execution.cli_launcher import build_cli_argv
from config.settings import settings


class CodexQuotaExhaustedError(Exception):
    """Codex explicitly reports its subscription usage or weekly allowance exhausted."""
    pass


class CodexExpert(BaseAgent):
    """
    高级顾问 Agent：调用本地 Codex CLI（现有订阅，可跳过/延后）。
    职责：解决 P1/P0 关键技术难题、处理复杂架构争议。
    特性：具备周额度耗尽检测与异常向上传递机制。
    """

    EXHAUSTION_PATTERNS = [
        r"\b(?:hit|reached|exceeded|exhausted)\b[^\n]{0,50}\b(?:usage\s+(?:limit|cap)|weekly\s+(?:usage\s+)?(?:limit|quota))\b",
        r"\b(?:usage\s+(?:limit|cap)|weekly\s+(?:usage\s+)?(?:limit|quota))\b[^\n]{0,50}\b(?:exhausted|exceeded|reached)\b",
        r"(?:周|每周)(?:使用)?(?:额度|限额).{0,12}(?:已耗尽|用完|超出)",
    ]

    def __init__(
        self,
        cli_cmd: str | None = None,
        mock_mode: bool = False,
        simulate_exhausted: bool = False,
        cli_argv_prefix: list[str] | None = None,
    ):
        super().__init__(role_name="SENIOR_EXPERT", model_name="Codex-CLI")
        self.cli_cmd = cli_cmd or settings.codex_cli_cmd
        self.cli_argv_prefix = (
            cli_argv_prefix if cli_argv_prefix is not None
            else settings.codex_cli_argv_prefix if cli_cmd is None else []
        )
        self.mock_mode = mock_mode
        self.simulate_exhausted = simulate_exhausted

    def check_is_quota_exhausted(self, text: str) -> bool:
        """Distinguish an explicit usage allowance failure from transient rate limits."""
        lowered = text.lower()
        return any(re.search(pattern, lowered) for pattern in self.EXHAUSTION_PATTERNS)

    @staticmethod
    def _mock_result(context: dict[str, Any]) -> AgentResult:
        metadata: dict[str, Any] = {"mock": True}
        if context.get("structured_review"):
            metadata["review"] = {
                "approved": True, "severity": "P3", "summary": "Explicit simulated audit",
                "critical_issues": [], "suggestions": [],
            }
        return AgentResult(
            success=True,
            content="[Mock Codex Senior Advisor] Add validated guard clauses and verify with tests.",
            metadata=metadata,
        )

    @staticmethod
    async def _verify_expected_head(cwd: Path, context: dict[str, Any]) -> None:
        """Bind backlog audits to the full recorded commit before invoking a model."""
        expected = context.get("expected_commit_sha")
        if expected is None:
            return
        if not isinstance(expected, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", expected):
            raise ValueError("expected_commit_sha must be a full Git commit SHA")
        head = await run_async_command(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=15.0)
        if not head.is_success or head.stdout.strip().casefold() != expected.casefold():
            raise ValueError("Task HEAD does not match the commit recorded for this audit")
        status = await run_async_command(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=cwd, timeout=15.0
        )
        if not status.is_success:
            raise ValueError("Cannot verify whether the recorded audit checkout is clean")
        if status.stdout.strip():
            raise ValueError("Recorded audit checkout has uncommitted or untracked changes")

    async def execute(self, prompt: str, context: dict[str, Any] | None = None) -> AgentResult:
        """Provide advice or a structured audit without modifying the task checkout."""
        start_time = time.monotonic()
        context = context or {}
        if self.simulate_exhausted:
            raise CodexQuotaExhaustedError("Codex weekly quota exhausted (simulated limit reached).")
        if self.mock_mode:
            return self._mock_result(context)
        issue_context = context.get("issue_context", "")
        full_prompt = f"Expert Advice Required:\nContext: {issue_context}\nTask: {prompt}"
        try:
            cwd = task_directory(context)
            await self._verify_expected_head(cwd, context)
            if context.get("structured_review"):
                full_prompt += f"\n{REVIEW_SCHEMA_PROMPT}"
            cmd = build_cli_argv(self.cli_cmd, self.cli_argv_prefix) + [
                "exec", "--sandbox", "read-only", "--ephemeral", "--color", "never", "-",
            ]
            auth_home = os.getenv("CODEX_HOME")
            credential_env = {"CODEX_HOME": auth_home} if auth_home else {}
            res: SubprocResult = await run_async_command(
                cmd, cwd=cwd, env=credential_env, timeout=300.0, input_text=full_prompt,
            )
            duration = time.monotonic() - start_time
            combined_output = f"{res.stdout}\n{res.stderr}"

            # 检测额度是否耗尽
            if not res.is_success and self.check_is_quota_exhausted(combined_output):
                raise CodexQuotaExhaustedError(
                    f"Codex quota exhausted detected from CLI: {combined_output[:200]}"
                )

            if not res.is_success:
                return AgentResult(
                    success=False,
                    content="",
                    duration_s=duration,
                    error=f"Codex CLI failed (exit {res.exit_code}): {res.stderr[:300]}"
                )

            if not res.stdout.strip():
                raise ValueError("Codex returned an empty response")
            await self._verify_expected_head(cwd, context)
            review = parse_review_output(res.stdout) if context.get("structured_review") else None
            return AgentResult(
                success=True,
                content=res.stdout,
                cost_cny=0.0,
                duration_s=duration,
                metadata={"exit_code": res.exit_code, "mock": False, **({"review": review} if review else {})}
            )

        except CodexQuotaExhaustedError:
            raise
        except Exception as e:
            duration = time.monotonic() - start_time
            return AgentResult(
                success=False,
                content="",
                duration_s=duration,
                error=f"Codex call error: {str(e)}"
            )
