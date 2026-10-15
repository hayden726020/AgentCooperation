"""Worker process that continuously polls Multica boards and dispatches issues."""

import asyncio
import logging
from typing import Callable, List, Optional

from integrations.multica.bridge import MulticaBridge
from integrations.multica.client import BaseMulticaClient
from integrations.multica.models import MulticaIssue, MulticaRunResult

logger = logging.getLogger("multica.worker")


class MulticaWorker:
    """Polls Multica issues assigned to this agent and processes them sequentially."""

    def __init__(
        self,
        bridge: MulticaBridge,
        poll_interval: int = 10,
        model: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
        filter_assignee: Optional[str] = None,
    ):
        self.bridge = bridge
        self.client: BaseMulticaClient = bridge.client
        self.poll_interval = max(1, poll_interval)
        self.model = model
        self.mock_mode = mock_mode
        self.custom_test_cmd = custom_test_cmd
        self.filter_assignee = filter_assignee
        self._running = False

    async def poll_once(self) -> List[MulticaRunResult]:
        """Fetch pending 'todo' issues and process them."""
        issues = await self.client.list_issues(status="todo", assignee=self.filter_assignee)
        results: List[MulticaRunResult] = []
        for issue in issues:
            logger.info("Found pending Multica issue: %s (%s)", issue.id, issue.title)
            try:
                res = await self.bridge.run_issue(
                    issue,
                    chosen_model=self.model,
                    mock_mode=self.mock_mode,
                    custom_test_cmd=self.custom_test_cmd,
                )
                results.append(res)
            except Exception as error:
                logger.error("Error processing issue %s: %s", issue.id, error, exc_info=True)
                try:
                    await self.client.update_issue_status(issue.id, status="blocked")
                    await self.client.post_comment(
                        issue.id,
                        f"💥 **系统异常阻断**: 执行任务时发生未处理错误: `{error}`",
                        author=self.bridge.assignee_name,
                    )
                except Exception:
                    pass
        return results

    async def run_forever(self, max_cycles: Optional[int] = None) -> None:
        """Keep polling until cancelled or max_cycles reached."""
        self._running = True
        cycles = 0
        while self._running:
            await self.poll_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
