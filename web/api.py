"""REST API routes for AgentCooperation Web Console."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from sqlmodel import select

from agents.codex_expert import CodexExpert
from config.settings import settings
from integrations.multica import MemoryMulticaClient, MulticaIssue
from orchestrator.cli import _core_scope, execute_multica_issue, execute_task, run_codex_backlog
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
