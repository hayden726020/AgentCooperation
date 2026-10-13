import re
import time
from typing import Dict, Any, Optional
from agents.base import BaseAgent, AgentResult
from execution.subproc import run_async_command, SubprocResult
from config.settings import settings


class CodexQuotaExhaustedError(Exception):
    """当 Codex 本地 CLI 遇到周额度耗尽、速率限制或配额不足时抛出"""
    pass


class CodexExpert(BaseAgent):
    """
    高级顾问 Agent：调用本地 Codex CLI（现有订阅，可跳过/延后）。
    职责：解决 P1/P0 关键技术难题、处理复杂架构争议。
    特性：具备周额度耗尽检测与异常向上传递机制。
    """

    EXHAUSTION_PATTERNS = [
        r"quota\s*exceeded",
        r"rate\s*limit",
        r"weekly\s*limit",
        r"usage\s*cap",
        r"insufficient_quota",
        r"status\s*429",
        r"out of quota",
        r"额度已耗尽",
        r"访问频率超限"
    ]

    def __init__(
        self,
        cli_cmd: Optional[str] = None,
        mock_mode: bool = False,
        simulate_exhausted: bool = False
    ):
        super().__init__(role_name="SENIOR_EXPERT", model_name="Codex-CLI")
        self.cli_cmd = cli_cmd or settings.codex_cli_cmd
        self.mock_mode = mock_mode
        self.simulate_exhausted = simulate_exhausted

    def check_is_quota_exhausted(self, text: str) -> bool:
        lowered = text.lower()
        for pat in self.EXHAUSTION_PATTERNS:
            if re.search(pat, lowered):
                return True
        return False

    async def execute(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        start_time = time.time()
        context = context or {}
        issue_context = context.get("issue_context", "")

        if self.simulate_exhausted:
            raise CodexQuotaExhaustedError("Codex weekly quota exhausted (simulated limit reached).")

        if self.mock_mode:
            duration = 0.5
            expert_advice = (
                "[Mock Codex Senior Advisor]\n"
                "Architecture Decision & Patch Guide:\n"
                "1. Root cause: The edge case was not accounted for during boundary validation.\n"
                "2. Recommended fix: Implement robust guard clauses before state transitions.\n"
                "3. Safe to proceed under Gemini supervision."
            )
            return AgentResult(
                success=True,
                content=expert_advice,
                cost_cny=0.0,
                duration_s=duration,
                metadata={"mock": True}
            )

        full_prompt = f"Expert Advice Required:\nContext: {issue_context}\nTask: {prompt}"
        cmd = [self.cli_cmd, "exec", "--sandbox", "read-only", full_prompt]

        try:
            res: SubprocResult = await run_async_command(cmd, timeout=300.0)
            duration = time.time() - start_time
            combined_output = f"{res.stdout}\n{res.stderr}"

            # 检测额度是否耗尽
            if self.check_is_quota_exhausted(combined_output):
                raise CodexQuotaExhaustedError(
                    f"Codex quota exhausted detected from CLI: {combined_output[:200]}"
                )

            if not res.is_success:
                # 检查退出码与报错中是否有额度耗尽特征
                if self.check_is_quota_exhausted(res.stderr):
                    raise CodexQuotaExhaustedError(f"Codex quota error: {res.stderr[:200]}")
                
                return AgentResult(
                    success=False,
                    content="",
                    duration_s=duration,
                    error=f"Codex CLI failed (exit {res.exit_code}): {res.stderr[:300]}"
                )

            return AgentResult(
                success=True,
                content=res.stdout,
                cost_cny=0.0,
                duration_s=duration,
                metadata={"exit_code": res.exit_code}
            )

        except CodexQuotaExhaustedError:
            raise
        except Exception as e:
            duration = time.time() - start_time
            return AgentResult(
                success=False,
                content="",
                duration_s=duration,
                error=f"Codex call error: {str(e)}"
            )
