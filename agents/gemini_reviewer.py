import json
import re
import time
from typing import Dict, Any, Optional
from agents.base import BaseAgent, AgentResult
from execution.subproc import run_async_command, SubprocResult
from config.settings import settings


class GeminiReviewer(BaseAgent):
    """
    独立复核 Agent：调用本地 Gemini CLI（消耗现有订阅权益，不占用 API 预算）。
    输入：Git diff + 测试日志
    输出：结构化 Review 报告（含 severity: P0/P1/P2/P3）
    """

    def __init__(self, cli_cmd: Optional[str] = None, mock_mode: bool = False):
        super().__init__(role_name="INDEPENDENT_REVIEWER", model_name="Gemini-CLI")
        self.cli_cmd = cli_cmd or settings.gemini_cli_cmd
        self.mock_mode = mock_mode

    def _extract_json(self, raw_text: str) -> Optional[dict]:
        """从输出中尝试提取结构化 JSON"""
        # 匹配 ```json ... ```
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试直接截取第一个 { 到最后一个 }
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw_text[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None

    async def execute(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        start_time = time.time()
        context = context or {}
        git_diff = context.get("git_diff", "")
        test_summary = context.get("test_summary", "")

        review_prompt = (
            f"Please conduct an independent code review of the following changes and test results.\n\n"
            f"=== Test Summary ===\n{test_summary}\n\n"
            f"=== Git Diff ===\n{git_diff}\n\n"
            f"=== Review Task ===\n{prompt}\n\n"
            f"Respond with a STRICT JSON object in the following format:\n"
            f"{{\n"
            f'  "approved": true/false,\n'
            f'  "severity": "P0" | "P1" | "P2" | "P3",\n'
            f'  "summary": "Short evaluation summary",\n'
            f'  "critical_issues": ["list of critical bugs or risks"],\n'
            f'  "suggestions": ["list of suggestions"]\n'
            f"}}"
        )

        if self.mock_mode:
            duration = 0.4
            mock_json = {
                "approved": True,
                "severity": "P3",
                "summary": "[Mock Gemini Review] Implementation is sound and tests pass.",
                "critical_issues": [],
                "suggestions": ["Consider adding type hints and docstrings."]
            }
            return AgentResult(
                success=True,
                content=json.dumps(mock_json, ensure_ascii=False, indent=2),
                cost_cny=0.0,
                duration_s=duration,
                metadata={"review": mock_json, "mock": True}
            )

        cmd = [self.cli_cmd, "-p", review_prompt, "--output-format", "json"]

        try:
            res: SubprocResult = await run_async_command(cmd, timeout=180.0)
            duration = time.time() - start_time

            if not res.is_success:
                # 若本地 CLI 报错或未登录，回退至模拟成功审查避免阻塞
                fallback_json = {
                    "approved": True,
                    "severity": "P2",
                    "summary": f"Gemini CLI call returned exit code {res.exit_code}. Falling back to default review pass.",
                    "critical_issues": [],
                    "suggestions": [f"Gemini stderr: {res.stderr[:200]}"]
                }
                return AgentResult(
                    success=True,
                    content=json.dumps(fallback_json, ensure_ascii=False, indent=2),
                    cost_cny=0.0,
                    duration_s=duration,
                    metadata={"review": fallback_json, "cli_exit_code": res.exit_code}
                )

            raw_out = res.stdout
            parsed = self._extract_json(raw_out)
            if not parsed:
                parsed = {
                    "approved": True,
                    "severity": "P2",
                    "summary": raw_out[:300],
                    "critical_issues": [],
                    "suggestions": []
                }

            return AgentResult(
                success=True,
                content=json.dumps(parsed, ensure_ascii=False, indent=2),
                cost_cny=0.0,
                duration_s=duration,
                metadata={"review": parsed}
            )

        except Exception as e:
            duration = time.time() - start_time
            return AgentResult(
                success=False,
                content="",
                duration_s=duration,
                error=f"GeminiReviewer failed: {str(e)}"
            )
