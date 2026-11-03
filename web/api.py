"""REST API routes for AgentCooperation Web Console."""

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, Response, jsonify, request, stream_with_context
from sqlmodel import select

from agents.codex_expert import CodexExpert
from config.settings import PROJECT_ROOT, settings
from execution.events import event_store
from integrations.multica import MemoryMulticaClient, MulticaIssue
from orchestrator.cli import _core_scope, execute_multica_issue, execute_task, run_codex_backlog
from web import run_service
from storage.db import (
    get_engine,
    get_or_create_codex_quota,
    get_or_create_monthly_spending,
    get_session,
)
from storage.models import (
    BudgetReservation,
    CodexQuotaRecord,
    ExecutionLog,
    MonthlySpending,
    PendingCodexAudit,
    Task,
)
from web.runner import job_manager

api_bp = Blueprint("api", __name__, url_prefix="/api")

# The console presents and accepts Codex recovery timestamps in Beijing time.
# A fixed UTC+08:00 offset is used instead of ``zoneinfo`` so the console works
# on fresh Windows environments that ship without the IANA tzdata package.
BEIJING_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc


def _is_mock(request_obj) -> bool:
    """Helper to detect mock mode flag from query params or JSON body.

    Only an explicit boolean ``true`` (or the query-string forms ``1``/``true``/
    ``yes``) selects the mock ledger. Any other value, including the string
    ``"false"``, is treated as live so a malformed flag can never silently pick
    the wrong database.
    """
    val = request_obj.args.get("mock")
    if val is not None:
        return val.strip().lower() in ("1", "true", "yes")
    if request_obj.is_json:
        body = request_obj.get_json(silent=True)
        if isinstance(body, dict):
            val = body.get("mock")
            if isinstance(val, bool):
                return val
    return False


def _serialize(obj: Any) -> Any:
    """Recursively serialize SQLModel instances and datetimes into JSON-safe dictionaries."""
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        d = obj.model_dump()
        return {k: _serialize(v) for k, v in d.items()}
    if hasattr(obj, "dict"):
        d = obj.dict()
        return {k: _serialize(v) for k, v in d.items()}
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a stored timestamp to aware UTC.

    Values written by this project are aware UTC (``storage.models.utc_now``),
    but SQLite round-trips may hand back naive values; those are interpreted as
    UTC, matching the project convention.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _beijing_iso(value: Optional[datetime]) -> Optional[str]:
    """Render a stored timestamp as a Beijing-time ISO string with +08:00 offset.

    ``None`` stays ``None`` so the UI can show "not set".
    """
    normalized = _as_utc(value)
    if normalized is None:
        return None
    return normalized.astimezone(BEIJING_TZ).isoformat()


def _parse_beijing_datetime(raw: str) -> datetime:
    """Parse an ISO-8601 string into an aware UTC datetime.

    A value without an explicit offset is interpreted as Beijing time, matching
    the CLI ``codex-quota --reset-at`` behaviour. Raises ``ValueError`` on
    malformed input or on values that overflow the representable range.
    """
    if not isinstance(raw, str):
        raise ValueError("estimated_recovery_at must be an ISO-8601 string or null")
    text = raw.strip()
    if not text:
        raise ValueError("estimated_recovery_at must not be empty")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 datetime: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    try:
        utc_dt = parsed.astimezone(UTC)
        if parsed.year >= 9999 or utc_dt.year >= 9999 or utc_dt.year < 1970:
            raise OverflowError("Datetime year out of supported range")
        return utc_dt
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"Datetime out of supported range: {exc}") from exc


def _strict_bool(value: Any, field: str) -> bool:
    """Return a strict boolean or raise ``ValueError``.

    Strings such as ``"false"`` are rejected instead of being coerced, so a
    malformed flag can never select the wrong ledger.
    """
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be a boolean")


@api_bp.route("/status", methods=["GET"])
def get_system_status():
    """Retrieve full orchestrator status and metrics for the dashboard."""
    mock = _is_mock(request)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            spending = get_or_create_monthly_spending(session)
            quota = get_or_create_codex_quota(session)
            audits = session.exec(
                select(PendingCodexAudit).where(
                    PendingCodexAudit.status.in_(["QUEUED", "AUDITING", "NEEDS_REPAIR"])
                )
            ).all()

            # Tasks statistics
            all_tasks = session.exec(select(Task)).all()
            total_tasks = len(all_tasks)
            success_tasks = sum(1 for t in all_tasks if t.state == "SUCCESS")
            failed_tasks = sum(1 for t in all_tasks if t.state in ("FAILED", "BUDGET_STOPPED", "INTERRUPTED"))
            active_tasks = sum(
                1 for t in all_tasks if t.state not in ("SUCCESS", "FAILED", "BUDGET_STOPPED", "INTERRUPTED")
            )

            policy = core.policy
            budget = policy.budget

            return jsonify({
                "mock_mode": mock,
                "db_mode": "mock" if mock else "live",
                "primary_model": policy.primary_agent,
                "spending": {
                    "year_month": spending.year_month,
                    "prompt_tokens": spending.total_prompt_tokens,
                    "completion_tokens": spending.total_completion_tokens,
                    "total_tokens": spending.total_prompt_tokens + spending.total_completion_tokens,
                    "total_cost_cny": round(spending.total_cost_cny, 5),
                    "monthly_limit_cny": budget.monthly_cny,
                    "warning_cny": budget.warning_cny,
                    "pause_new_cny": budget.pause_new_cny,
                    "percent_used": min(100.0, round((spending.total_cost_cny / budget.monthly_cny) * 100, 2))
                    if budget.monthly_cny > 0
                    else 0,
                },
                "codex_quota": {
                    "state": quota.state,
                    "last_checked_at": _serialize(quota.last_checked_at),
                    "estimated_recovery_at": _beijing_iso(quota.estimated_recovery_at),
                    "exhausted_reason": quota.exhausted_reason,
                    "enabled": policy.codex.enabled,
                },
                "audits": {
                    "pending_count": len(audits),
                },
                "tasks_count": {
                    "total": total_tasks,
                    "success": success_tasks,
                    "failed": failed_tasks,
                    "active": active_tasks,
                },
            })


@api_bp.route("/tasks", methods=["GET"])
def list_tasks():
    """List tasks with optional filtering by status and primary model."""
    mock = _is_mock(request)
    limit = int(request.args.get("limit", 100))
    state_filter = request.args.get("state")
    model_filter = request.args.get("model")

    with _core_scope(mock=mock) as core:
        with core._session() as session:
            stmt = select(Task).order_by(Task.id.desc())
            if state_filter:
                stmt = stmt.where(Task.state == state_filter)
            if model_filter:
                stmt = stmt.where(Task.primary_model == model_filter)
            stmt = stmt.limit(limit)
            tasks = session.exec(stmt).all()
            return jsonify({
                "mock_mode": mock,
                "total": len(tasks),
                "tasks": [_serialize(t) for t in tasks],
            })


@api_bp.route("/tasks/<task_id>", methods=["GET"])
def get_task_detail(task_id: str):
    """Retrieve details for a specific task including full execution logs."""
    mock = _is_mock(request)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                return jsonify({"error": f"Task '{task_id}' not found"}), 404

            logs = session.exec(
                select(ExecutionLog)
                .where(ExecutionLog.task_id == task_id)
                .order_by(ExecutionLog.id.asc())
            ).all()

            # Also check if there's any pending audit record for this task
            audits = session.exec(
                select(PendingCodexAudit).where(PendingCodexAudit.task_id == task_id)
            ).all()

            return jsonify({
                "mock_mode": mock,
                "task": _serialize(task),
                "logs": [_serialize(log) for log in logs],
                "audits": [_serialize(a) for a in audits],
            })


@api_bp.route("/tasks", methods=["POST"])
def create_task():
    """Trigger a new orchestration task asynchronously."""
    data = request.get_json() or {}
    requirement = data.get("requirement") or data.get("task")
    if not requirement or not requirement.strip():
        return jsonify({"error": "Task requirement is required"}), 400

    title = data.get("title") or requirement[:40]
    model = data.get("model") or "glm"
    if model not in ("glm", "deepseek"):
        return jsonify({"error": "Model must be either 'glm' or 'deepseek'"}), 400

    mock = bool(data.get("mock", False))
    repo_root = Path(data["repo_root"]) if data.get("repo_root") else None
    test_command = data.get("test_command") or None

    job_id = job_manager.create_job(
        "run_task",
        metadata={
            "title": title,
            "model": model,
            "mock": mock,
            "requirement": requirement,
            "repo_root": str(repo_root) if repo_root else None,
            "test_command": test_command,
        },
    )

    async def _run():
        res = await execute_task(
            requirement,
            model=model,
            mock=mock,
            title=title,
            repo_root=repo_root,
            test_command=test_command,
            quiet=True,
        )
        return {
            "task_id": res.task_id,
            "state": res.state,
            "total_cost_cny": res.total_cost_cny,
            "branch_name": res.branch_name,
            "summary": res.result_summary,
        }

    job_manager.run_async(job_id, _run)

    return jsonify({
        "success": True,
        "job_id": job_id,
        "message": "Task queued for execution",
        "mock_mode": mock,
    }), 202


@api_bp.route("/jobs", methods=["GET"])
def list_jobs():
    """List recent background jobs and their execution states."""
    limit = int(request.args.get("limit", 20))
    return jsonify({"jobs": job_manager.list_jobs(limit=limit)})


@api_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    """Query execution status of a background job."""
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": f"Job '{job_id}' not found"}), 404
    return jsonify(job)


@api_bp.route("/multica/board", methods=["GET"])
def get_multica_board():
    """Retrieve Multica Kanban issues and recent comments."""
    board_file = request.args.get("board_file")
    path = Path(board_file) if board_file else Path("data/multica_board.json")
    client = MemoryMulticaClient(persistence_file=path)

    issues = list(client._issues.values())
    comments = [c for c_list in client._comments.values() for c in c_list]
    comments.sort(key=lambda x: x.created_at, reverse=True)

    # Group issues by Kanban columns
    columns = {
        "todo": [],
        "in_progress": [],
        "in_review": [],
        "done": [],
        "blocked": [],
        "failed": [],
    }
    for issue in issues:
        status = issue.status or "todo"
        if status in columns:
            columns[status].append(_serialize(issue))
        else:
            columns.setdefault(status, []).append(_serialize(issue))

    return jsonify({
        "board_file": str(path),
        "total_issues": len(issues),
        "total_comments": len(comments),
        "columns": columns,
        "recent_comments": [_serialize(c) for c in comments[:30]],
    })


@api_bp.route("/multica/run", methods=["POST"])
def run_multica_issue():
    """Claim and execute a Multica issue via AgentCooperation."""
    data = request.get_json() or {}
    issue_id = data.get("issue_id")
    if not issue_id:
        return jsonify({"error": "issue_id is required"}), 400

    title = data.get("title")
    task_req = data.get("task") or data.get("requirement")
    model = data.get("model") or "glm"
    mock = bool(data.get("mock", False))
    repo_root = Path(data["repo_root"]) if data.get("repo_root") else None
    test_command = data.get("test_command")
    board_file = data.get("board_file")

    job_id = job_manager.create_job(
        "multica_run",
        metadata={
            "issue_id": issue_id,
            "title": title,
            "model": model,
            "mock": mock,
        },
    )

    async def _run():
        res = await execute_multica_issue(
            issue_id=issue_id,
            title=title,
            task_req=task_req,
            model=model,
            mock=mock,
            repo_root=repo_root,
            test_command=test_command,
            persistence_file=board_file,
            quiet=True,
        )
        return {
            "issue_id": res.issue_id,
            "task_id": res.task_id,
            "multica_status": res.multica_status,
            "fsm_state": res.fsm_state,
            "total_cost_cny": res.total_cost_cny,
            "summary": res.summary,
        }

    job_manager.run_async(job_id, _run)

    return jsonify({
        "success": True,
        "job_id": job_id,
        "message": f"Multica issue {issue_id} queued for execution",
    }), 202


@api_bp.route("/multica/issue", methods=["POST"])
def create_multica_issue():
    """Create a new issue on the local Multica Kanban board."""
    data = request.get_json() or {}
    title = data.get("title")
    description = data.get("description") or data.get("task")
    if not title or not description:
        return jsonify({"error": "Title and description are required"}), 400

    issue_id = data.get("id") or f"ISS-{int(datetime.now().timestamp()) % 100000}"
    priority = data.get("priority", "high")
    board_file = data.get("board_file")
    path = Path(board_file) if board_file else Path("data/multica_board.json")

    client = MemoryMulticaClient(persistence_file=path)
    issue = MulticaIssue(
        id=issue_id,
        title=title,
        description=description,
        status="todo",
        priority=priority,
    )
    asyncio.run(client.create_issue(issue))

    return jsonify({"success": True, "issue": _serialize(issue)}), 201


@api_bp.route("/codex/quota", methods=["GET"])
def get_codex_quota():
    """Get Codex quota record and all pending backlog audits."""
    mock = _is_mock(request)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            quota = get_or_create_codex_quota(session)
            audits = session.exec(
                select(PendingCodexAudit).order_by(PendingCodexAudit.id.desc())
            ).all()

            return jsonify({
                "mock_mode": mock,
                "quota": {
                    "state": quota.state,
                    "last_checked_at": _serialize(quota.last_checked_at),
                    "estimated_recovery_at": _beijing_iso(quota.estimated_recovery_at),
                    "exhausted_reason": quota.exhausted_reason,
                },
                "audits": [_serialize(a) for a in audits],
            })


@api_bp.route("/codex/quota", methods=["POST"])
def update_codex_quota():
    """Manually update Codex quota state and estimated recovery timestamp."""
    data = request.get_json() or {}
    state = data.get("state")
    if state not in ("READY", "RESERVED", "EXHAUSTED", "RECOVERING"):
        return jsonify({"error": "Invalid state. Must be READY, RESERVED, EXHAUSTED, or RECOVERING"}), 400

    reset_at_str = data.get("reset_at")
    reset_at = None
    if reset_at_str:
        try:
            reset_at = datetime.fromisoformat(reset_at_str)
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=UTC)
        except Exception as e:
            return jsonify({"error": f"Invalid reset_at ISO format: {e}"}), 400

    mock = bool(data.get("mock", False))
    with _core_scope(mock=mock) as core:
        core.codex_guard.set_quota_state(
            state, reason=data.get("reason", "Operator web override"), recovery_at=reset_at
        )

    return jsonify({"success": True, "state": state, "reset_at": _serialize(reset_at)})


@api_bp.route("/codex/recovery", methods=["POST"])
def update_codex_recovery():
    """Set or clear only the Codex estimated recovery timestamp.

    This endpoint intentionally touches only ``estimated_recovery_at`` so a
    concurrent state change (for example a recovery probe flipping the state to
    READY) is never clobbered by a stale form value. It does not trigger any
    Codex probe or backlog audit, and it never promotes the quota to READY.

    Request body:
        mock (bool, optional): select the mock or live ledger. Must be a strict
            boolean; strings such as ``"false"`` are rejected.
        estimated_recovery_at (str|null): ISO-8601 timestamp. A value without an
            offset is interpreted as Beijing time (UTC+08:00). ``null`` clears
            the stored timestamp.

    Responses:
        200: ``{success, mock_mode, estimated_recovery_at, state, exhausted_reason}``
        400: ``{error}`` for invalid dates or JSON types; existing data is untouched.
    """
    if not request.is_json:
        return jsonify({"error": "Request body must be JSON"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    try:
        mock = _strict_bool(data.get("mock", False), "mock")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if "estimated_recovery_at" not in data:
        return jsonify({"error": "estimated_recovery_at is required (use null to clear)"}), 400

    raw_value = data.get("estimated_recovery_at")
    if raw_value is None:
        normalized = None
    elif isinstance(raw_value, str):
        try:
            normalized = _parse_beijing_datetime(raw_value)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        return jsonify({
            "error": "estimated_recovery_at must be an ISO-8601 string or null"
        }), 400

    with _core_scope(mock=mock) as core:
        with core._session() as session:
            record = session.exec(
                select(CodexQuotaRecord).order_by(CodexQuotaRecord.id.desc())
            ).first()
            if record is None:
                record = CodexQuotaRecord()
            record.estimated_recovery_at = normalized
            session.add(record)
            session.commit()
            session.refresh(record)
            state = record.state
            reason = record.exhausted_reason
            stored = record.estimated_recovery_at

    return jsonify({
        "success": True,
        "mock_mode": mock,
        "estimated_recovery_at": _beijing_iso(stored),
        "state": state,
        "exhausted_reason": reason,
    })


@api_bp.route("/codex/backlog/trigger", methods=["POST"])
def trigger_codex_backlog():
    """Trigger Codex backlog audit processing asynchronously."""
    data = request.get_json() or {}
    mock = bool(data.get("mock", False))

    job_id = job_manager.create_job("codex_backlog", metadata={"mock": mock})

    async def _run():
        return await run_codex_backlog(mock_mode=mock)

    job_manager.run_async(job_id, _run)

    return jsonify({
        "success": True,
        "job_id": job_id,
        "message": "Codex backlog processing triggered",
    }), 202


@api_bp.route("/budget", methods=["GET"])
def get_budget_details():
    """Get monthly spending breakdown and recent budget reservations."""
    mock = _is_mock(request)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            spending = get_or_create_monthly_spending(session)
            reservations = session.exec(
                select(BudgetReservation).order_by(BudgetReservation.id.desc()).limit(50)
            ).all()

            policy = core.policy
            return jsonify({
                "mock_mode": mock,
                "spending": _serialize(spending),
                "limits": _serialize(policy.budget),
                "reservations": [_serialize(r) for r in reservations],
            })


@api_bp.route("/policy", methods=["GET"])
def get_policy():
    """Get active system policy configurations (pricing, guards, limits)."""
    policy = settings.load_policy()
    return jsonify({
        "primary_agent": policy.primary_agent,
        "reviewer_agent": policy.reviewer_agent,
        "expert_agent": policy.expert_agent,
        "models": _serialize(policy.models),
        "budget": _serialize(policy.budget),
        "codex": _serialize(policy.codex),
        "repair": _serialize(policy.repair),
        "execution": _serialize(policy.execution),
    })


def _run_task_summary(task: Task) -> Dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "state": task.state,
        "mock_mode": task.mock_mode,
        "repo_root": task.repo_root,
        "reviewer_model": task.reviewer_model,
        "repair_attempts": task.repair_attempts,
        "commit_sha": task.commit_sha,
        "test_passed": task.test_passed,
        "prompt_tokens": task.total_prompt_tokens,
        "completion_tokens": task.total_completion_tokens,
        "cost_cny": round(task.total_cost_cny or 0.0, 5),
        "result_summary": task.result_summary,
        "created_at": _serialize(task.created_at),
        "updated_at": _serialize(task.updated_at),
        "completed_at": _serialize(task.completed_at),
    }


@api_bp.route("/runs/options", methods=["GET"])
def get_run_options():
    """Expose whitelisted repositories and smart defaults for the run console."""
    policy = settings.load_policy()
    repos = run_service.available_repos(PROJECT_ROOT, policy)
    project = str(PROJECT_ROOT.resolve())
    default_repo = next((item["path"] for item in repos if item["path"] == project), None)
    if default_repo is None and repos:
        default_repo = repos[0]["path"]
    return jsonify({
        "default_repo": default_repo or project,
        "repos": repos,
        "reviewers": list(run_service.REVIEWER_CHOICES),
        "default_reviewer": "glm",
        "max_repair_attempts": int(getattr(policy.repair, "max_normal_attempts", 3)),
        "max_repair_limit": run_service.MAX_REPAIR_LIMIT,
        "allowed_repo_patterns": list(policy.security.allowed_repos or ["."]),
    })


@api_bp.route("/runs", methods=["GET"])
def list_runs():
    """List closed-loop runs persisted in the task ledger, newest first."""
    mock = _is_mock(request)
    try:
        limit = int(request.args.get("limit", 50))
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    state_filter = request.args.get("state")
    repo_filter = (request.args.get("repo") or "").strip()
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            stmt = (
                select(Task)
                .where(Task.primary_model == run_service.CLOSED_LOOP_MODEL)
                .order_by(Task.id.desc())
                .limit(limit)
            )
            if state_filter:
                stmt = stmt.where(Task.state == state_filter)
            if repo_filter:
                repo_path = Path(repo_filter)
                if not repo_path.is_absolute():
                    repo_path = PROJECT_ROOT / repo_path
                stmt = stmt.where(Task.repo_root == str(repo_path.resolve()))
            tasks = session.exec(stmt).all()
            return jsonify({
                "mock_mode": mock,
                "total": len(tasks),
                "runs": [_run_task_summary(task) for task in tasks],
            })


@api_bp.route("/runs/<task_id>", methods=["GET"])
def get_run_detail(task_id: str):
    """Return one run's task record, execution logs and event cursor."""
    mock = _is_mock(request)
    with _core_scope(mock=mock) as core:
        with core._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                return jsonify({"error": f"Run '{task_id}' not found"}), 404
            logs = session.exec(
                select(ExecutionLog)
                .where(ExecutionLog.task_id == task_id)
                .order_by(ExecutionLog.id.asc())
            ).all()
            last_seq = event_store.last_seq(task_id)
            recent = event_store.read(task_id, after_seq=max(0, last_seq - 20), limit=20)
            terminal = next((event for event in reversed(recent) if event.is_terminal), None)
            return jsonify({
                "mock_mode": mock,
                "run": _run_task_summary(task),
                "task": _serialize(task),
                "logs": [_serialize(log) for log in logs],
                "events": {
                    "last_seq": last_seq,
                    "terminal": terminal.to_dict() if terminal else None,
                },
            })


@api_bp.route("/runs", methods=["POST"])
def create_run():
    """Submit a closed-loop run with smart defaults and streamable events."""
    if not request.is_json:
        return jsonify({"error": "Request body must be JSON"}), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    mock = data.get("mock", False)
    if not isinstance(mock, bool):
        return jsonify({"error": "mock must be a boolean"}), 400
    allow_untrusted = data.get("allow_untrusted_tests", False)
    if not isinstance(allow_untrusted, bool):
        return jsonify({"error": "allow_untrusted_tests must be a boolean"}), 400
    if allow_untrusted:
        # The web surface never grants untrusted test execution; only the
        # operator-facing CLI may opt in.
        return jsonify({"error": "allow_untrusted_tests is not permitted from the web console"}), 403

    try:
        info = run_service.submit_run(
            requirement=data.get("requirement") or "",
            project_root=PROJECT_ROOT,
            policy=settings.load_policy(),
            repo=data.get("repo"),
            reviewer=data.get("reviewer"),
            max_repair=data.get("max_repair"),
            test_paths=data.get("test_paths"),
            title=data.get("title"),
            allow_untrusted_tests=False,
            mock=mock,
            store=event_store,
        )
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except run_service.RunValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"success": True, "message": "Run queued", **info}), 202


def _submit_approval(task_id: str, approved: bool):
    """Validate one GUI approval decision and dispatch the resumed run."""
    mock = _is_mock(request)
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        return jsonify({"error": "notes must be a string"}), 400
    if isinstance(notes, str) and len(notes) > 2000:
        return jsonify({"error": "notes must be at most 2000 characters"}), 400
    allow_untrusted = data.get("allow_untrusted_tests", False)
    if not isinstance(allow_untrusted, bool):
        return jsonify({"error": "allow_untrusted_tests must be a boolean"}), 400
    if not approved:
        allow_untrusted = False

    with _core_scope(mock=mock) as core:
        with core._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                return jsonify({"error": f"Run '{task_id}' not found"}), 404
            if task.primary_model != run_service.CLOSED_LOOP_MODEL:
                return jsonify({"error": "Only closed-loop runs support GUI approval"}), 400
            if bool(task.mock_mode) != mock:
                return jsonify({
                    "error": "Run mode does not match the requested ledger; refresh the console",
                }), 409
            if task.state != "WAITING_APPROVAL":
                return jsonify({
                    "error": f"Run '{task_id}' is in state '{task.state}', not 'WAITING_APPROVAL'",
                    "state": task.state,
                }), 409
            repo_root = (task.repo_root or "").strip()
            if approved and not repo_root:
                return jsonify({
                    "error": "Run has no recorded repository; resume it from the CLI",
                }), 409
            reviewer_model = task.reviewer_model
            mock_mode = bool(task.mock_mode)

    try:
        info = run_service.start_approval(
            task_id=task_id,
            approved=approved,
            notes=notes,
            allow_untrusted_tests=allow_untrusted,
            repo_root=repo_root,
            reviewer_model=reviewer_model,
            mock_mode=mock_mode,
            project_root=PROJECT_ROOT,
            policy=settings.load_policy(),
            store=event_store,
        )
    except run_service.RunConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except run_service.RunValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    action = "approved" if approved else "rejected"
    return jsonify({"success": True, "message": f"Run {action}", **info}), 202


@api_bp.route("/runs/<task_id>/approve", methods=["POST"])
def approve_run(task_id: str):
    """Approve a paused closed-loop run and resume it."""
    return _submit_approval(task_id, True)


@api_bp.route("/runs/<task_id>/reject", methods=["POST"])
def reject_run(task_id: str):
    """Reject a paused closed-loop run and mark it FAILED."""
    return _submit_approval(task_id, False)


def _parse_after_seq(raw: Any) -> int:
    try:
        value = int(raw if raw not in (None, "") else 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("after must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("after must be a non-negative integer")
    return value


def _parse_idle_timeout(raw: Any) -> float:
    if raw in (None, ""):
        return 30.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("idle_timeout must be a number") from exc
    return max(0.1, min(value, 120.0))


def _parse_heartbeat(raw: Any) -> float:
    if raw in (None, ""):
        return 15.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("heartbeat must be a number") from exc
    return max(0.1, min(value, 60.0))


def _sse_event_name(event_type: str) -> str:
    """Keep the SSE ``event:`` line on a single field-safe token."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", event_type)[:64] or "message"


@api_bp.route("/runs/<task_id>/events", methods=["GET"])
def get_run_events(task_id: str):
    """Return persisted run events after a sequence number (polling fallback)."""
    try:
        after = _parse_after_seq(request.args.get("after"))
        limit = int(request.args.get("limit", 500))
        if limit < 1:
            raise ValueError("limit must be a positive integer")
        events = event_store.read(task_id, after_seq=after, limit=min(limit, 2000))
        last_seq = event_store.last_seq(task_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "task_id": task_id,
        "events": [event.to_dict() for event in events],
        "last_seq": last_seq,
    })


@api_bp.route("/runs/<task_id>/events/stream", methods=["GET"])
def stream_run_events(task_id: str):
    """Stream run events as Server-Sent Events, resumable via Last-Event-ID."""
    try:
        event_store.path_for(task_id)
        after = _parse_after_seq(
            request.args.get("after") or request.headers.get("Last-Event-ID")
        )
        idle_timeout = _parse_idle_timeout(request.args.get("idle_timeout"))
        heartbeat = _parse_heartbeat(request.args.get("heartbeat"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # A finished run needs no long-lived connection: EventSource treats 204 as
    # "stop reconnecting" and the client already has every event up to ``after``.
    if after > 0:
        last = event_store.read(task_id, after_seq=after - 1, limit=1)
        if last and last[0].seq == after and last[0].is_terminal:
            return Response(status=204)

    stop = threading.Event()

    def generate():
        try:
            yield "retry: 3000\n\n"
            for event in event_store.tail(
                task_id,
                after_seq=after,
                idle_timeout=idle_timeout,
                heartbeat_interval=heartbeat,
                stop_event=stop,
            ):
                if event is None:
                    yield ": ping\n\n"
                    continue
                data = json.dumps(event.to_dict(), ensure_ascii=False)
                yield f"id: {event.seq}\nevent: {_sse_event_name(event.type)}\ndata: {data}\n\n"
        finally:
            # A disconnected client must not pin this worker until idle_timeout.
            stop.set()

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
