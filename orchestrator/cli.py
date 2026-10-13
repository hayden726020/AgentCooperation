"""CLI for single-Flash tasks, isolated simulations and deferred audit supervision."""

import argparse
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlmodel import select

from agents.codex_expert import CodexExpert
from config.settings import settings
from orchestrator.core import OrchestratorCore
from storage.db import get_engine, get_or_create_codex_quota, get_or_create_monthly_spending, get_session
from storage.models import PendingCodexAudit, Task

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

console = Console(highlight=False)


@contextmanager
def _core_scope(mock=False, repo_root=None):
    if not mock:
        yield OrchestratorCore(repo_root=repo_root)
        return
    path = Path(settings.sqlite_db_path)
    mock_path = path.with_name(f"{path.stem}.mock{path.suffix or '.db'}")
    engine = get_engine(str(mock_path))
    try:
        with get_session(engine) as session:
            yield OrchestratorCore(repo_root=repo_root, session=session)
    finally:
        engine.dispose()


def show_status(mock=False):
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            spending = get_or_create_monthly_spending(session)
            quota = get_or_create_codex_quota(session)
            audits = session.exec(select(PendingCodexAudit).where(
                PendingCodexAudit.status.in_(["QUEUED", "AUDITING", "NEEDS_REPAIR"]))).all()
            budget = core.policy.budget
            console.print(Panel(
                f"账库: {'独立模拟账库（模拟成本见任务历史）' if mock else '真实调用估算账库'}\n"
                f"月份: {spending.year_month}\n"
                f"输入/输出 Token: {spending.total_prompt_tokens:,} / {spending.total_completion_tokens:,}\n"
                f"API 保守估算支出: ¥{spending.total_cost_cny:.5f} / ¥{budget.monthly_cny:.2f}\n"
                f"本地 Codex 状态: {quota.state}（不是账户实时额度查询）\n"
                f"待补审/待修复: {len(audits)}\n"
                f"预计恢复时间: {quota.estimated_recovery_at or '未设置'}",
                title="运行状态", border_style="cyan"))


def list_tasks(mock=False):
    table = Table(title="模拟任务历史" if mock else "任务历史")
    for label in ("Task ID", "标题", "引擎", "状态", "修复次数", "费用估算(¥)", "待补审"):
        table.add_column(label)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            tasks = session.exec(select(Task).order_by(Task.id.desc()).limit(15)).all()
            for task in tasks:
                table.add_row(task.task_id, task.title[:25], task.primary_model, task.state,
                              str(task.repair_attempts), f"{task.total_cost_cny:.5f}",
                              "是" if task.has_pending_codex_audit else "否")
    console.print(table)


def _print_backlog(results):
    if not results:
        console.print("没有可领取的待补审记录；任务运行中或待人工修复的记录会保留。")
        return
    table = Table(title="补审结果")
    for name in ("Audit ID", "任务", "状态", "反馈"):
        table.add_column(name)
    for result in results:
        table.add_row(result["audit_id"], result["task_id"], result["status"], result["feedback"])
    console.print(table)


async def run_codex_backlog(mock_mode=False):
    with _core_scope(mock=mock_mode) as core:
        results = await core.codex_guard.process_backlog(CodexExpert(mock_mode=mock_mode),
                                                        allow_simulated=mock_mode)
        _print_backlog(results)
        return results


async def watch_codex_backlog(mock_mode=False, interval=300, cycles=None):
    if interval < 30:
        raise ValueError("Recovery checks must be at least 30 seconds apart")
    count = 0
    with _core_scope(mock=mock_mode) as core:
        expert = CodexExpert(mock_mode=mock_mode)
        while True:
            try:
                if core.codex_guard.get_pending_audits() and await core.codex_guard.probe_recovery(
                        expert, worktree_path=Path.cwd(), allow_simulated=mock_mode):
                    results = await core.codex_guard.process_backlog(expert, allow_simulated=mock_mode)
                    if results:
                        _print_backlog(results)
            except Exception as error:
                console.print(f"补审检查失败，记录保留：{type(error).__name__}")
            count += 1
            if cycles is not None and count >= cycles:
                return
            await asyncio.sleep(interval)


async def execute_task(req, model, mock, title, *, repo_root=None, test_command=None):
    model = model or settings.load_policy().primary_agent
    console.print(Panel(
        f"需求: {req}\n引擎: {model}（单任务二选一）\n"
        f"模式: {'显式模拟，使用独立账库' if mock else '真实调用'}",
        title="启动任务", border_style="cyan"))
    with _core_scope(mock=mock, repo_root=repo_root) as core:
        task = await core.run_task(req, title=title, chosen_model=model, mock_mode=mock,
                                   custom_test_cmd=test_command)
    console.print(Panel(
        f"任务: {task.task_id}\n状态: {task.state}\n费用估算: ¥{task.total_cost_cny:.5f}\n"
        f"工作目录: {task.worktree_path}\n{task.result_summary}",
        title="模拟结果" if mock else "任务结果",
        border_style="green" if task.state == "SUCCESS" else "yellow"))
    return task


def _reset_time(value):
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use an ISO timestamp, e.g. 2026-10-20T12:00:00+08:00") from error
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone(timedelta(hours=8)))
    return result.astimezone(timezone.utc)


def main():
    parser = argparse.ArgumentParser(description="单 Flash API + Gemini/Codex CLI 调度器")
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="执行任务")
    run_parser.add_argument("--task", "-t", required=True)
    run_parser.add_argument("--title", default=None)
    run_parser.add_argument("--model", "-m", choices=["glm", "deepseek"], default=None)
    run_parser.add_argument("--mock", action="store_true")
    run_parser.add_argument("--repo-root", type=Path, default=None, help="已有初始提交的目标 Git 项目")
    run_parser.add_argument("--test-command", default=None, help="明确指定目标验收命令")
    for command in ("status", "tasks", "codex-backlog"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--mock", action="store_true")
    watcher = subparsers.add_parser("codex-watch", help="按恢复时间自动探测并补审")
    watcher.add_argument("--mock", action="store_true")
    watcher.add_argument("--interval", type=int, default=300)
    quota = subparsers.add_parser("codex-quota", help="设置本地额度状态与官方恢复时间")
    quota.add_argument("--state", required=True, choices=["READY", "RESERVED", "EXHAUSTED", "RECOVERING"])
    quota.add_argument("--reset-at", type=_reset_time, default=None, help="无时区时按北京时间处理")
    quota.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "run":
            task = asyncio.run(execute_task(args.task, args.model, args.mock, args.title,
                                           repo_root=args.repo_root, test_command=args.test_command))
            return 0 if task.state == "SUCCESS" else 2
        if args.command == "status":
            show_status(args.mock)
        elif args.command == "tasks":
            list_tasks(args.mock)
        elif args.command == "codex-backlog":
            results = asyncio.run(run_codex_backlog(args.mock))
            return 2 if any(r["status"] not in {"RESOLVED", "SIMULATED"} for r in results) else 0
        elif args.command == "codex-watch":
            asyncio.run(watch_codex_backlog(args.mock, args.interval))
        elif args.command == "codex-quota":
            with _core_scope(mock=args.mock) as core:
                core.codex_guard.set_quota_state(args.state, reason="Operator override", recovery_at=args.reset_at)
        else:
            parser.print_help()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        console.print(f"执行失败：{error}", style="red", markup=False)
        return 2


if __name__ == "__main__":
    sys.exit(main())
