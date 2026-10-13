import argparse
import asyncio
import sys
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from sqlmodel import select

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from storage.db import get_session, init_db, get_or_create_monthly_spending, get_or_create_codex_quota
from storage.models import Task, PendingCodexAudit
from orchestrator.core import OrchestratorCore
from agents.codex_expert import CodexExpert

console = Console(highlight=False)


def show_status():
    """打印系统当前状态：月度预算、Codex 周额度状态、待补审队列"""
    init_db()
    with get_session() as s:
        spending = get_or_create_monthly_spending(s)
        codex_quota = get_or_create_codex_quota(s)
        pending_audits = s.exec(select(PendingCodexAudit).where(PendingCodexAudit.status == "QUEUED")).all()

    # 1. 预算面板
    budget_panel = Panel(
        f"[bold cyan]当前月份:[/bold cyan] {spending.year_month}\n"
        f"[bold green]累计消耗 Prompt Tokens:[/bold green] {spending.total_prompt_tokens:,}\n"
        f"[bold green]累计消耗 Completion Tokens:[/bold green] {spending.total_completion_tokens:,}\n"
        f"[bold yellow]总支出 (API):[/bold yellow] ¥{spending.total_cost_cny:.4f} / ¥200.00\n"
        f"[bold blue]安全预算余量:[/bold blue] ¥{max(0.0, 200.0 - spending.total_cost_cny):.4f}",
        title="[Budget Guard] API 预算状态 (GLM-5.3-Flash / DeepSeek-Flash)",
        border_style="yellow"
    )
    console.print(budget_panel)

    # 2. Codex 状态
    quota_color = "green" if codex_quota.state == "READY" else ("red" if codex_quota.state == "EXHAUSTED" else "yellow")
    codex_panel = Panel(
        f"[bold]状态机状态:[/bold] [{quota_color}]{codex_quota.state}[/{quota_color}]\n"
        f"[bold]待补审队列数:[/bold] {len(pending_audits)} 个重大问题待恢复补审\n"
        f"[bold]上次检测时间:[/bold] {codex_quota.last_checked_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"[bold]耗尽原因/说明:[/bold] {codex_quota.exhausted_reason or '无'}",
        title="[Codex Guard] 周额度守护与待补审状态",
        border_style=quota_color
    )
    console.print(codex_panel)



def list_tasks():
    init_db()
    with get_session() as s:
        tasks = s.exec(select(Task).order_by(Task.id.desc()).limit(15)).all()

    table = Table(title="[Tasks] 任务执行历史")
    table.add_column("Task ID", style="cyan")
    table.add_column("标题", style="white")
    table.add_column("主力引擎", style="magenta")
    table.add_column("状态", style="bold")
    table.add_column("自修次数", style="yellow")
    table.add_column("Token 总数", style="green")
    table.add_column("费用 (¥)", style="yellow")
    table.add_column("待补审", style="red")

    for t in tasks:
        state_style = "green" if t.state == "SUCCESS" else ("red" if t.state == "FAILED" else "yellow")
        table.add_row(
            t.task_id[:16],
            t.title[:25],
            t.primary_model,
            f"[{state_style}]{t.state}[/{state_style}]",
            str(t.repair_attempts),
            f"{(t.total_prompt_tokens + t.total_completion_tokens):,}",
            f"¥{t.total_cost_cny:.4f}",
            "[!] 需补审" if t.has_pending_codex_audit else "正常"
        )

    console.print(table)


async def run_codex_backlog(mock_mode: bool = False):
    """处理待补审队列"""
    core = OrchestratorCore()
    console.print("[bold cyan][*] 正在触发 Codex 待补审队列处理...[/bold cyan]")
    expert = CodexExpert(mock_mode=mock_mode)
    results = await core.codex_guard.process_backlog(expert)

    if not results:
        console.print("[green][OK] 暂无待补审任务，队列为空。[/green]")
        return

    table = Table(title="Codex 补审处理结果")
    table.add_column("Audit ID", style="cyan")
    table.add_column("关联任务", style="magenta")
    table.add_column("处理状态", style="bold")
    table.add_column("专家反馈摘要", style="white")

    for r in results:
        table.add_row(
            r["audit_id"],
            r["task_id"][:16],
            r["status"],
            r["feedback"]
        )

    console.print(table)


async def execute_task(req: str, model: str, mock: bool, title: str):
    core = OrchestratorCore()
    console.print(Panel(
        f"[bold]任务需求:[/bold] {req}\n"
        f"[bold]主力模型:[/bold] {model} (互斥独占单引擎模式)\n"
        f"[bold]执行模式:[/bold] {'Mock 模拟演练' if mock else '真实线上调用'}",
        title="[Start] 正在启动全自动多 Agent 任务编排",
        border_style="cyan"
    ))

    task = await core.run_task(
        requirement=req,
        title=title,
        chosen_model=model,
        mock_mode=mock
    )

    color = "green" if task.state == "SUCCESS" else "red"
    console.print(Panel(
        f"[bold]任务 ID:[/bold] {task.task_id}\n"
        f"[bold]最终状态:[/bold] [{color}]{task.state}[/{color}]\n"
        f"[bold]自修尝试:[/bold] {task.repair_attempts} 次\n"
        f"[bold]消耗 Token:[/bold] {task.total_prompt_tokens + task.total_completion_tokens:,}\n"
        f"[bold]任务总支出:[/bold] ¥{task.total_cost_cny:.4f}\n"
        f"[bold]Codex 待补审:[/bold] {'是 (周额度耗尽已优雅排队)' if task.has_pending_codex_audit else '否'}\n"
        f"[bold]工作空间目录:[/bold] {task.worktree_path}",
        title="[Complete] 任务编排结束",
        border_style=color
    ))



def main():
    parser = argparse.ArgumentParser(description="GLM & DeepSeek Flash 协同编排自动化引擎")
    subparsers = parser.add_subparsers(dest="command")

    # run command
    run_parser = subparsers.add_parser("run", help="启动执行自动化编程任务")
    run_parser.add_argument("--task", "-t", required=True, help="任务描述")
    run_parser.add_argument("--title", default=None, help="任务简短标题")
    run_parser.add_argument("--model", "-m", choices=["glm", "deepseek"], default="glm", help="主力 Flash 代码模型 (二选一)")
    run_parser.add_argument("--mock", action="store_true", help="启用 Mock 模拟演练（不调用真实 API）")

    # status command
    subparsers.add_parser("status", help="查看月度预算与 Codex 额度状态")

    # tasks command
    subparsers.add_parser("tasks", help="查看任务执行历史")

    # codex-backlog command
    backlog_parser = subparsers.add_parser("codex-backlog", help="重试处理 Codex 待补审队列")
    backlog_parser.add_argument("--mock", action="store_true", help="使用模拟 Codex 专家")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(execute_task(args.task, args.model, args.mock, args.title))
    elif args.command == "status":
        show_status()
    elif args.command == "tasks":
        list_tasks()
    elif args.command == "codex-backlog":
        asyncio.run(run_codex_backlog(args.mock))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
