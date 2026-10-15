"""Bridge connecting Multica issues to AgentCooperation OrchestratorCore."""

from pathlib import Path
from typing import Optional, Union

from integrations.multica.client import BaseMulticaClient
from integrations.multica.models import (
    MulticaIssue,
    MulticaRunResult,
    map_fsm_state_to_multica_status,
)
from orchestrator.core import OrchestratorCore
from storage.models import Task


class MulticaBridge:
    """Orchestrates the lifecycle between Multica boards and AgentCooperation pipeline."""

    def __init__(
        self,
        client: BaseMulticaClient,
        core: OrchestratorCore,
        assignee_name: str = "AgentCooperation",
    ):
        self.client = client
        self.core = core
        self.assignee_name = assignee_name

    async def run_issue(
        self,
        issue_or_id: Union[MulticaIssue, str],
        chosen_model: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
    ) -> MulticaRunResult:
        """Claim a Multica issue, run the full pipeline in an isolated worktree, and report results."""
        if isinstance(issue_or_id, str):
            issue = await self.client.get_issue(issue_or_id)
            if not issue:
                raise ValueError(f"Multica issue '{issue_or_id}' could not be found.")
        else:
            issue = issue_or_id

        # 1. Claim the issue and set status to in_progress
        await self.client.claim_issue(issue.id, assignee=self.assignee_name)

        model_name = chosen_model or self.core.policy.primary_agent
        init_comment = (
            f"🤖 **AgentCooperation 任务已认领**\n\n"
            f"- **分配执行人**: `{self.assignee_name}`\n"
            f"- **主编程引擎**: `{model_name}` (单任务专用 Flash 实例)\n"
            f"- **审查模式**: Gemini CLI 独立复核 + Codex CLI 疑难兜底\n"
            f"- **运行模式**: {'[模拟运行]' if mock_mode else '[真实模型调用]'}\n"
            f"- **隔离环境**: 正在创建专用 Git Worktree 沙箱..."
        )
        await self.client.post_comment(issue.id, init_comment, author=self.assignee_name)

        # 2. Run the core orchestration pipeline
        task = await self.core.run_task(
            requirement=issue.description,
            title=issue.title,
            chosen_model=chosen_model,
            mock_mode=mock_mode,
            custom_test_cmd=custom_test_cmd,
        )

        # 3. Map FSM state to Multica issue status
        multica_status = map_fsm_state_to_multica_status(task.state)

        # 4. Generate comprehensive Markdown report for Multica timeline
        report = self._build_report_comment(task, multica_status, mock_mode)
        await self.client.post_comment(issue.id, report, author=self.assignee_name)

        # 5. Update issue status and branch on Multica board
        await self.client.update_issue_status(
            issue.id,
            status=multica_status,
            branch_name=task.branch_name,
        )

        return MulticaRunResult(
            issue_id=issue.id,
            task_id=task.task_id,
            fsm_state=task.state,
            multica_status=multica_status,
            total_cost_cny=task.total_cost_cny,
            branch_name=task.branch_name,
            summary=task.result_summary or "",
            comments_posted=[init_comment, report],
        )

    def _build_report_comment(self, task: Task, multica_status: str, mock_mode: bool) -> str:
        status_icons = {
            "done": "✅",
            "in_review": "🔍",
            "blocked": "⛔",
            "in_progress": "🔄",
        }
        icon = status_icons.get(multica_status, "ℹ️")

        lines = [
            f"### {icon} AgentCooperation 执行报告 (任务状态: `{task.state}` -> Multica: `{multica_status}`)",
            "",
            f"- **Task ID**: `{task.task_id}`",
            f"- **开发分支**: `{task.branch_name or 'N/A'}`",
            f"- **工作树路径**: `{task.worktree_path or 'N/A'}`",
            f"- **修复轮次**: `{task.repair_attempts}`",
            f"- **费用估算**: ¥{task.total_cost_cny:.5f} {'(模拟账库)' if mock_mode else ''}",
            f"- **待补审标记**: {'是 (Codex 额度耗尽暂存)' if task.has_pending_codex_audit else '否'}",
            "",
            "#### 阶段总结与产物",
            task.result_summary or "无详细总结",
        ]

        if task.state == "WAITING_APPROVAL":
            lines.extend([
                "",
                "> ⚠️ **人工审批提醒 (Blocked)**: 检测到关键风险或严重问题，已自动阻断合并流程并暂停专家补丁，等待人工决策。",
            ])
        elif task.state == "FAILED":
            lines.extend([
                "",
                "> ❌ **执行失败 (Blocked)**: 自修轮次耗尽或关键测试未通过，请查阅执行日志进行人工排查。",
            ])

        return "\n".join(lines)
