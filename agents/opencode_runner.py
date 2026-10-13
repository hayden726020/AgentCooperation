import time
from typing import Dict, Any, Optional
from agents.base import BaseAgent, AgentResult
from execution.subproc import run_async_command, SubprocResult
from config.settings import settings


class OpenCodeRunner(BaseAgent):
    """
    可选的 OpenCode CLI 适配器。
    用于在需要完整终端沙箱交互时，直接拉起 `opencode run` 执行任务。
    """

    def __init__(
        self,
        model_name: str,
        agent_type: str = "build",  # "build" 或 "plan"
        cli_cmd: Optional[str] = None
    ):
        super().__init__(role_name="OPENCODE_RUNNER", model_name=model_name)
        self.agent_type = agent_type
        self.cli_cmd = cli_cmd or settings.opencode_cli_cmd

    async def execute(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        start_time = time.time()
        context = context or {}
        worktree_path = context.get("worktree_path")

        cmd = [
            self.cli_cmd,
            "run",
            "--model", self.model_name,
            "--agent", self.agent_type,
            prompt
        ]

        try:
            res: SubprocResult = await run_async_command(
                cmd=cmd,
                cwd=worktree_path,
                timeout=600.0
            )
            duration = time.time() - start_time

            if not res.is_success:
                return AgentResult(
                    success=False,
                    content="",
                    duration_s=duration,
                    error=f"OpenCode error (exit {res.exit_code}): {res.stderr[:300]}"
                )

            return AgentResult(
                success=True,
                content=res.stdout,
                duration_s=duration,
                metadata={"exit_code": res.exit_code}
            )
        except Exception as e:
            duration = time.time() - start_time
            return AgentResult(
                success=False,
                content="",
                duration_s=duration,
                error=f"Failed to run OpenCode: {str(e)}"
            )
