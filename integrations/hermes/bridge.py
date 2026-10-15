"""Hermes Cooperation Bridge: Connects Hermes Agent host to AgentCooperation OrchestratorCore.

Provides:
- Persistent request deduplication (idempotency)
- Authorized repository validation (allowed_repos)
- Hard budget pre-checks and reservations
- Task submission, progress querying, result reading, and pending approval handling
- Direct role bot capabilities (Primary Dev, Independent Reviewer, Codex Expert)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from sqlmodel import Session, select

from agents.base import AgentResult
from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from config.settings import PolicyConfig, settings
from integrations.hermes.models import (
    HermesApprovalRequest,
    HermesApprovalResponse,
    HermesBudgetStatus,
    HermesRoleExecutionRequest,
    HermesRoleExecutionResponse,
    HermesSubmitRequest,
    HermesTaskResponse,
)
from orchestrator.budget_guard import BudgetGuard, BudgetLimitError
from orchestrator.codex_guard import CodexQuotaGuard
from orchestrator.core import OrchestratorCore
from orchestrator.engine_selector import SingleEngineSlotEnforcer
from storage.db import (
    get_or_create_codex_quota,
    get_or_create_monthly_spending,
    init_db,
    locked_session,
    provide_session,
)
from storage.models import (
    BudgetReservation,
    CodexQuotaRecord,
    ExecutionLog,
    IdempotencyRecord,
    MonthlySpending,
    PendingCodexAudit,
    Task,
    utc_now,
)

logger = logging.getLogger(__name__)


class HermesCooperationBridge:
    """High-level bridge between Hermes agent and AgentCooperation orchestrator."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        session: Optional[Session] = None,
        policy: Optional[PolicyConfig] = None,
    ):
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.session = session
        self.policy = policy or settings.load_policy()
        init_db(session.get_bind() if session is not None else None)
        self.core = OrchestratorCore(repo_root=self.repo_root, session=self.session)
        # Ensure core uses same policy if explicitly passed
        if policy:
            self.core.policy = policy
            self.core.budget_guard = BudgetGuard(policy.budget, session)
            self.core.codex_guard = CodexQuotaGuard(policy.codex, session)

    def _provide_session(self):
        return provide_session(self.session)

    # -------------------------------------------------------------------------
    # 1. Security & Authorized Repository Validation
    # -------------------------------------------------------------------------
    def validate_repo_root(self, repo_root: Optional[Union[str, Path]]) -> Path:
        """Validate that repo_root is authorized, exists, and is a valid Git repository."""
        clean_root = repo_root.strip('\'" \t\r\n') if isinstance(repo_root, str) else repo_root
        target = Path(clean_root) if clean_root else self.repo_root
        if not target.is_absolute():
            target = (self.repo_root / target).resolve()
        else:
            target = target.resolve()

        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Target repository directory does not exist: {target}")

        # Check allowed_repos
        allowed_list = self.policy.security.allowed_repos or ["."]
        resolved_allowed = []
        for item in allowed_list:
            p = Path(item)
            if not p.is_absolute():
                resolved_allowed.append((self.repo_root / p).resolve())
            else:
                resolved_allowed.append(p.resolve())

        is_authorized = False
        for allowed in resolved_allowed:
            try:
                if target == allowed or target.is_relative_to(allowed):
                    is_authorized = True
                    break
            except (ValueError, RuntimeError):
                continue

        if not is_authorized:
            raise PermissionError(
                f"Unauthorized repository path: '{target}'. "
                f"Must be within authorized roots: {[str(p) for p in resolved_allowed]}"
            )

        # Validate git repository status
        dot_git = target / ".git"
        if not dot_git.exists():
            raise ValueError(f"Directory is not a Git repository (no .git found): {target}")

        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0:
                raise ValueError(f"Target is not inside a valid Git worktree: {target}")
            # Ensure repository has at least an initial commit
            rev_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(target),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if rev_res.returncode != 0:
                raise ValueError(f"Target Git repository has no initial commit: {target}")
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"Git check timed out for target repository: {target}")

        return target

    # -------------------------------------------------------------------------
    # 2. Idempotency Key Computation & Lookup
    # -------------------------------------------------------------------------
    def compute_idempotency_key(
        self,
        repo_path: Path,
        requirement: str,
        chosen_model: str,
        custom_key: Optional[str] = None,
        mock_mode: bool = False,
    ) -> str:
        """Derive or normalize a persistent deduplication key."""
        if custom_key and custom_key.strip():
            return f"key_{custom_key.strip()}"
        mode_str = "mock" if mock_mode else "live"
        payload = f"{repo_path.as_posix()}::{chosen_model}::{mode_str}::{requirement.strip()}"
        return f"auto_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def _get_idempotency_record(self, key: str) -> Optional[IdempotencyRecord]:
        with self._provide_session() as s:
            return s.exec(
                select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == key)
            ).first()

    # -------------------------------------------------------------------------
    # 3. Task Submission with Deduplication & Hard Budget Interception
    # -------------------------------------------------------------------------
    async def submit_task(self, request: HermesSubmitRequest) -> HermesTaskResponse:
        """Submit a task to the orchestrator pipeline with deduplication and hard budget protection."""
        if not request.requirement or not request.requirement.strip():
            raise ValueError("Task requirement must not be empty")

        # 1. Authorize repository
        target_repo = self.validate_repo_root(request.repo_root)
        model_name = request.chosen_model or self.policy.primary_agent
        if model_name not in ("glm", "deepseek"):
            raise ValueError(f"Invalid primary model '{model_name}'. Must be 'glm' or 'deepseek'.")

        # 2. Compute idempotency key
        req_hash = hashlib.sha256(request.requirement.strip().encode()).hexdigest()
        idem_key = self.compute_idempotency_key(
            target_repo, request.requirement, model_name, request.idempotency_key, mock_mode=request.mock_mode
        )

        # 3. Check persistent deduplication record
        with locked_session(self.session) as s:
            existing_record = s.exec(
                select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == idem_key)
            ).first()

            if existing_record:
                # Custom key verification: ensure compatible request fingerprint
                rec_mock = getattr(existing_record, "mock_mode", False)
                if (
                    existing_record.repo_root != str(target_repo)
                    or existing_record.chosen_model != model_name
                    or existing_record.requirement_hash != req_hash
                    or rec_mock != request.mock_mode
                ):
                    raise ValueError(
                        f"Custom idempotency key '{idem_key}' was previously used with incompatible "
                        f"request parameters (repo, model, mock_mode, or requirement). Reuse across conflicting requests is rejected."
                    )

                # If previously rejected by budget, re-check whether budget has recovered
                if existing_record.status == "REJECTED":
                    if not request.mock_mode:
                        can_start, _ = self.core.budget_guard.check_can_start_new_task()
                        if can_start:
                            s.delete(existing_record)
                            s.commit()
                            existing_record = None
                    else:
                        s.delete(existing_record)
                        s.commit()
                        existing_record = None

            if existing_record:
                # If already completed or rejected, return cached response
                if existing_record.status in ("COMPLETED", "REJECTED", "FAILED") and existing_record.response_payload_json:
                    try:
                        cached_dict = json.loads(existing_record.response_payload_json)
                        cached_dict["deduplicated"] = True
                        return HermesTaskResponse(**cached_dict)
                    except Exception:
                        pass
                # If still in progress, query active task state
                if existing_record.task_id and existing_record.task_id != "pending":
                    task = s.exec(select(Task).where(Task.task_id == existing_record.task_id)).first()
                    if task:
                        response = self._build_task_response(task, deduplicated=True)
                        return response

            # 4. Hard budget interception BEFORE creating task or reserving budget
            if not request.mock_mode:
                can_start, budget_msg = self.core.budget_guard.check_can_start_new_task()
                if not can_start:
                    rejected_resp = HermesTaskResponse(
                        task_id="",
                        status="REJECTED",
                        state="BUDGET_STOPPED",
                        title=request.title or request.requirement[:40],
                        requirement=request.requirement,
                        primary_model=model_name,
                        error=budget_msg,
                        deduplicated=False,
                    )
                    # Record rejected submission
                    rejected_record = IdempotencyRecord(
                        idempotency_key=idem_key,
                        task_id="",
                        repo_root=str(target_repo),
                        requirement_hash=req_hash,
                        chosen_model=model_name,
                        mock_mode=request.mock_mode,
                        status="REJECTED",
                        response_payload_json=json.dumps(rejected_resp.model_dump(), ensure_ascii=False),
                        error_message=budget_msg,
                    )
                    s.add(rejected_record)
                    return rejected_resp

            # Generate upfront real task_id to avoid "pending" races and support queries & restarts
            real_task_id = f"task_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            initial_task = Task(
                task_id=real_task_id,
                title=request.title or request.requirement[:40],
                requirement=request.requirement,
                primary_model=model_name,
                branch_name=f"codex/{real_task_id}",
                state="INIT",
            )
            s.add(initial_task)

            # Register in-progress idempotency record with real task_id
            in_progress_record = IdempotencyRecord(
                idempotency_key=idem_key,
                task_id=real_task_id,
                repo_root=str(target_repo),
                requirement_hash=req_hash,
                chosen_model=model_name,
                mock_mode=request.mock_mode,
                status="IN_PROGRESS",
            )
            s.add(in_progress_record)

        # 5. Run the core orchestrator pipeline
        self.core.repo_root = target_repo
        self.core.worktree_mgr = self.core.worktree_mgr.__class__(
            target_repo, self.policy.execution.git_worktree_base
        )

        task_result: Optional[Task] = None
        execution_error: Optional[str] = None
        try:
            task_result = await self.core.run_task(
                requirement=request.requirement,
                title=request.title,
                chosen_model=model_name,
                mock_mode=request.mock_mode,
                custom_test_cmd=request.custom_test_cmd,
                task_id=real_task_id,
            )
        except Exception as exc:
            execution_error = str(exc)
            logger.exception("Task execution encountered error: %s", exc)

        # 6. Settle and update IdempotencyRecord
        with locked_session(self.session) as s:
            record = s.exec(
                select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == idem_key)
            ).first()

            if task_result:
                resp = self._build_task_response(task_result, deduplicated=False)
                if record:
                    record.task_id = task_result.task_id
                    record.status = "COMPLETED" if task_result.state == "SUCCESS" else task_result.state
                    record.response_payload_json = json.dumps(resp.model_dump(), ensure_ascii=False)
                    record.updated_at = utc_now()
                    s.add(record)
                return resp
            else:
                fail_resp = HermesTaskResponse(
                    task_id=real_task_id,
                    status="FAILED",
                    state="FAILED",
                    title=request.title or request.requirement[:40],
                    requirement=request.requirement,
                    primary_model=model_name,
                    error=execution_error or "Unknown orchestrator failure",
                    deduplicated=False,
                )
                if record:
                    record.status = "FAILED"
                    record.error_message = execution_error
                    record.response_payload_json = json.dumps(fail_resp.model_dump(), ensure_ascii=False)
                    record.updated_at = utc_now()
                    s.add(record)
                return fail_resp

    # -------------------------------------------------------------------------
    # 4. Status Querying & Artifact Extraction
    # -------------------------------------------------------------------------
    def query_task(self, task_id: str) -> Optional[HermesTaskResponse]:
        """Query detailed task progress, evidence, and results by task_id."""
        with self._provide_session() as s:
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                return None
            return self._build_task_response(task)

    def list_tasks(
        self,
        limit: int = 20,
        status_filter: Optional[str] = None,
        mock_mode: Optional[bool] = None,
    ) -> List[HermesTaskResponse]:
        """List tasks with optional filtering."""
        with self._provide_session() as s:
            stmt = select(Task).order_by(Task.id.desc()).limit(limit)
            if status_filter:
                stmt = stmt.where(Task.state == status_filter)
            tasks = s.exec(stmt).all()
            return [self._build_task_response(t) for t in tasks]

    def _build_task_response(self, task: Task, deduplicated: bool = False) -> HermesTaskResponse:
        """Construct a complete HermesTaskResponse with logs, test, and review summaries."""
        test_summary: Optional[str] = None
        review_summary: Optional[Dict[str, Any]] = None
        diff_summary: Optional[str] = None
        files_written: List[str] = []

        with self._provide_session() as s:
            logs = s.exec(
                select(ExecutionLog).where(ExecutionLog.task_id == task.task_id).order_by(ExecutionLog.id)
            ).all()

            for log in logs:
                if log.agent_role == "TEST_RUNNER" and log.output_snippet:
                    test_summary = log.output_snippet
                elif log.agent_role == "REVIEWER" and log.output_snippet:
                    try:
                        review_summary = json.loads(log.output_snippet)
                    except Exception:
                        review_summary = {"raw": log.output_snippet}

            # Check pending audits for diff or issue context
            audit = s.exec(
                select(PendingCodexAudit).where(PendingCodexAudit.task_id == task.task_id)
            ).first()
            if audit:
                diff_summary = audit.git_diff_summary

        # Identify files generated in worktree if path exists
        if task.worktree_path and Path(task.worktree_path).exists():
            wt = Path(task.worktree_path)
            for f in ("TASK.md", "SPEC.json"):
                if (wt / f).exists():
                    files_written.append(f)

        return HermesTaskResponse(
            task_id=task.task_id,
            status=task.state,
            state=task.state,
            title=task.title,
            requirement=task.requirement,
            primary_model=task.primary_model,
            branch_name=task.branch_name,
            worktree_path=task.worktree_path,
            total_cost_cny=task.total_cost_cny,
            repair_attempts=task.repair_attempts,
            codex_calls=task.codex_calls,
            has_pending_codex_audit=task.has_pending_codex_audit,
            result_summary=task.result_summary,
            test_summary=test_summary,
            review_summary=review_summary,
            diff_summary=diff_summary,
            files_written=files_written,
            deduplicated=deduplicated,
            created_at=task.created_at.isoformat() if task.created_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None,
        )

    # -------------------------------------------------------------------------
    # 5. Pending Approvals & Backlog Management
    # -------------------------------------------------------------------------
    def get_pending_approvals(self) -> Dict[str, Any]:
        """Fetch all tasks pending human approval (e.g. P0 risk) and queued Codex audits."""
        with self._provide_session() as s:
            p0_tasks = s.exec(
                select(Task).where(Task.state == "WAITING_APPROVAL").order_by(Task.id.desc())
            ).all()

            pending_audits = s.exec(
                select(PendingCodexAudit).where(PendingCodexAudit.status == "QUEUED").order_by(PendingCodexAudit.id)
            ).all()

            return {
                "p0_waiting_approval": [self._build_task_response(t).model_dump() for t in p0_tasks],
                "queued_codex_audits": [
                    {
                        "audit_id": a.audit_id,
                        "task_id": a.task_id,
                        "severity": a.severity,
                        "issue_description": a.issue_description,
                        "commit_sha": a.commit_sha,
                        "created_at": a.created_at.isoformat() if a.created_at else None,
                    }
                    for a in pending_audits
                ],
            }

    async def approve_task(self, request: HermesApprovalRequest) -> HermesApprovalResponse:
        """Approve or reject a task in WAITING_APPROVAL state via OrchestratorCore.

        Human approval authorizes subsequent processing; the task must still pass
        necessary repair, testing, and independent review before reaching SUCCESS.
        """
        task = None
        target_mock_mode = False
        with self._provide_session() as s:
            task = s.exec(select(Task).where(Task.task_id == request.task_id)).first()
        if not task:
            mock_db_path = settings.PROJECT_ROOT / "data/mock_agent_orchestrator.db"
            if mock_db_path.exists():
                mock_engine = get_engine(str(mock_db_path))
                with Session(mock_engine) as ms:
                    task = ms.exec(select(Task).where(Task.task_id == request.task_id)).first()
                    if task:
                        target_mock_mode = True

        if not task:
            return HermesApprovalResponse(
                task_id=request.task_id,
                success=False,
                message=f"Task '{request.task_id}' not found.",
                updated_state="",
            )

        if task.state != "WAITING_APPROVAL":
            return HermesApprovalResponse(
                task_id=request.task_id,
                success=False,
                message=f"Task '{request.task_id}' is in state '{task.state}', not 'WAITING_APPROVAL'.",
                updated_state=task.state,
            )

        mock_mode = request.mock_mode if request.mock_mode is not None else (target_mock_mode or task.mock_mode)

        if task.primary_model == "gemini-3.8-flash-high":
            from orchestrator.gemini_closed_loop import GeminiClosedLoopOrchestrator
            rev_choice = "deepseek" if "deepseek" in (task.reviewer_model or "") else "glm"
            loop = GeminiClosedLoopOrchestrator(
                repo_root=self.repo_root,
                session=self.session,
                policy=self.policy,
                reviewer_choice=rev_choice,
            )
            closed_result = await loop.resume_after_approval(
                task_id=request.task_id,
                approved=request.approved,
                notes=request.notes,
                mock_mode=mock_mode,
            )
            return HermesApprovalResponse(
                task_id=task.task_id,
                success=closed_result.success,
                message=f"Task '{task.task_id}' processed to '{closed_result.state}' ({closed_result.result_summary}).",
                updated_state=closed_result.state,
            )

        try:
            resumed_task = await self.core.resume_task_after_approval(
                task_id=request.task_id,
                approved=request.approved,
                notes=request.notes,
                mock_mode=mock_mode,
            )
            new_state = resumed_task.state
            decision_text = resumed_task.result_summary or (
                "Approved by human operator" if request.approved else "Rejected by human operator"
            )

            # If there's an associated pending audit, close or update it
            with locked_session(self.session) as s:
                audit = s.exec(
                    select(PendingCodexAudit).where(PendingCodexAudit.task_id == task.task_id)
                ).first()
                if audit:
                    audit.status = "RESOLVED" if request.approved and new_state == "SUCCESS" else "CLOSED"
                    audit.resolved_at = utc_now()
                    audit.codex_feedback = f"Approval action applied: {decision_text}"
                    s.add(audit)

            return HermesApprovalResponse(
                task_id=task.task_id,
                success=True,
                message=f"Task '{task.task_id}' processed to '{new_state}' ({decision_text}).",
                updated_state=new_state,
            )
        except Exception as exc:
            logger.exception("Error during task approval execution: %s", exc)
            return HermesApprovalResponse(
                task_id=request.task_id,
                success=False,
                message=f"Error executing approval flow for '{request.task_id}': {exc}",
                updated_state="FAILED",
            )

    # -------------------------------------------------------------------------
    # 6. Budget Status & Quota Inspection
    # -------------------------------------------------------------------------
    def check_budget_status(self) -> HermesBudgetStatus:
        """Inspect monthly spending, reservations, warnings, and Codex quota machine."""
        with self._provide_session() as s:
            spending = get_or_create_monthly_spending(s)
            month = spending.year_month
            reservations = s.exec(
                select(BudgetReservation).where(
                    BudgetReservation.status == "RESERVED",
                    BudgetReservation.year_month == month,
                    BudgetReservation.simulated == False,
                )
            ).all()
            pending_total = sum(r.estimated_cost_cny for r in reservations)
            projected = spending.total_cost_cny + pending_total

            can_start, msg = self.core.budget_guard.check_can_start_new_task()
            codex_quota = get_or_create_codex_quota(s)

            return HermesBudgetStatus(
                monthly_cny=self.policy.budget.monthly_cny,
                current_spending_cny=spending.total_cost_cny,
                pending_reservations_cny=pending_total,
                total_projected_cny=projected,
                warning_cny=self.policy.budget.warning_cny,
                pause_new_cny=self.policy.budget.pause_new_cny,
                hard_stop_cny=self.policy.budget.hard_stop_cny,
                task_limit_cny=self.policy.budget.task_limit_cny,
                can_start_new_task=can_start,
                budget_message=msg,
                codex_quota_state=codex_quota.state,
                codex_recovery_at=codex_quota.estimated_recovery_at.isoformat()
                if codex_quota.estimated_recovery_at
                else None,
            )

    # -------------------------------------------------------------------------
    # 7. Direct Role Bot Capabilities (Multi-Bot Division of Labor)
    # -------------------------------------------------------------------------
    async def run_role_dev_plan(
        self,
        requirement: str,
        repo_root: Optional[str] = None,
        chosen_model: Optional[str] = None,
        mock_mode: bool = False,
    ) -> HermesRoleExecutionResponse:
        """Invoke Primary Dev Agent (GLM or DeepSeek Flash) for planning in an isolated worktree."""
        target_repo = self.validate_repo_root(repo_root)
        model_name = chosen_model or self.policy.primary_agent
        if model_name not in ("glm", "deepseek"):
            raise ValueError(f"Primary engine must be 'glm' or 'deepseek', got '{model_name}'")

        primary = SingleEngineSlotEnforcer(self.policy).get_primary_agent(model_name, mock_mode=mock_mode)
        prompt = f"Plan this task and write TASK.md and SPEC.json with objective acceptance tests:\n{requirement}"

        # Create isolated worktree to avoid writing directly to the main repository
        sync_task_id = f"plan_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self.core.repo_root = target_repo
        self.core.worktree_mgr = self.core.worktree_mgr.__class__(
            target_repo, self.policy.execution.git_worktree_base
        )
        worktree = (await self.core.worktree_mgr.create_worktree(sync_task_id)).resolve()

        context = {
            "worktree_path": worktree,
            "project_context": self.core._project_context(target_repo),
            "max_output_tokens": self.policy.execution.max_output_tokens,
        }

        # Check and reserve budget for this invocation
        estimate = primary.estimate_max_cost(prompt, context)
        with locked_session(self.session) as s:
            s.add(Task(
                task_id=sync_task_id,
                title="Dev Plan",
                requirement=requirement,
                primary_model=model_name,
                branch_name=f"codex/{sync_task_id}",
                worktree_path=str(worktree),
                state="PLANNING",
            ))

        res_id = self.core.budget_guard.reserve_call(sync_task_id, estimate, simulated=mock_mode)
        try:
            result: AgentResult = await primary.execute(prompt, context=context)
            self.core.budget_guard.settle_call(
                res_id,
                result.prompt_tokens,
                result.completion_tokens,
                cost_cny=None if result.metadata.get("usage_missing") else result.cost_cny,
            )

            terminal_state = "SUCCESS" if result.success else "FAILED"
            with locked_session(self.session) as s:
                plan_task = s.exec(select(Task).where(Task.task_id == sync_task_id)).first()
                if plan_task:
                    plan_task.state = terminal_state
                    plan_task.total_cost_cny = result.cost_cny
                    plan_task.completed_at = utc_now()
                    plan_task.result_summary = "Dev Plan generated in isolated worktree" if result.success else result.error
                    s.add(plan_task)
                s.add(ExecutionLog(
                    task_id=sync_task_id,
                    step_name="Dev Plan Generation",
                    agent_role="PRIMARY_DEV",
                    agent_model=model_name,
                    status=terminal_state,
                    cost_cny=result.cost_cny,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    output_snippet=result.content[:500] if result.content else result.error,
                ))

            return HermesRoleExecutionResponse(
                role=f"Primary Dev ({model_name})",
                success=result.success,
                content=result.content,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_cny=result.cost_cny,
                error=result.error,
                metadata=result.metadata,
            )
        except Exception as exc:
            self.core.budget_guard.settle_call(res_id, 0, 0, cost_cny=0.0)
            with locked_session(self.session) as s:
                plan_task = s.exec(select(Task).where(Task.task_id == sync_task_id)).first()
                if plan_task:
                    plan_task.state = "FAILED"
                    plan_task.completed_at = utc_now()
                    plan_task.result_summary = str(exc)
                    s.add(plan_task)
            raise

    async def run_role_reviewer(
        self,
        worktree_path: str,
        requirement: str,
        git_diff: str = "",
        test_summary: str = "",
        mock_mode: bool = False,
    ) -> HermesRoleExecutionResponse:
        """Invoke Independent Reviewer (Antigravity/Gemini/Codex CLI) on code and tests."""
        target_repo = self.validate_repo_root(worktree_path)
        wt = Path(worktree_path).resolve()
        if not wt.is_dir():
            raise ValueError(f"Invalid worktree path for review: {wt}")

        # Resolve full Git commit SHA required by independent review agents
        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head_res.returncode != 0:
            raise ValueError(f"Failed to resolve full commit SHA for review at {wt}: {head_res.stderr.strip()}")
        full_commit_sha = head_res.stdout.strip()

        reviewer = self.core._get_reviewer(mock_mode=mock_mode)
        prompt = "Review requirements, code and actual test evidence."
        context = {
            "worktree_path": wt,
            "git_diff": git_diff,
            "test_summary": test_summary,
            "requirement": requirement,
            "expected_commit_sha": full_commit_sha,
        }

        result: AgentResult = await reviewer.execute(prompt, context=context)
        return HermesRoleExecutionResponse(
            role=f"Reviewer ({getattr(reviewer, 'model_name', self.policy.reviewer_agent)})",
            success=result.success,
            content=result.content,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_cny=result.cost_cny,
            error=result.error,
            metadata=result.metadata,
        )

    async def run_role_expert(
        self,
        issue_context: str,
        worktree_path: str,
        mock_mode: bool = False,
    ) -> HermesRoleExecutionResponse:
        """Invoke Codex Expert for architecture, hard bug diagnosis, or repair plan under policy controls."""
        target_repo = self.validate_repo_root(worktree_path)
        wt = Path(worktree_path).resolve()

        if not self.policy.codex.enabled:
            raise PermissionError("Codex Expert is disabled in policy configuration.")

        if not self.core.codex_guard.is_available():
            raise CodexQuotaExhaustedError("Codex Expert quota is currently exhausted.")

        expert = CodexExpert(mock_mode=mock_mode)
        prompt = "Diagnose the issue and provide a verifiable repair plan."
        context = {
            "worktree_path": wt,
            "issue_context": issue_context,
        }

        result: AgentResult = await expert.execute(prompt, context=context)
        return HermesRoleExecutionResponse(
            role="Codex Expert",
            success=result.success,
            content=result.content,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_cny=result.cost_cny,
            error=result.error,
            metadata=result.metadata,
        )

    async def run_gemini_closed_loop(
        self,
        requirement: str,
        repo_root: Optional[str] = None,
        title: Optional[str] = None,
        reviewer_choice: Literal["glm", "deepseek"] = "glm",
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
        test_paths: Optional[list[str]] = None,
        max_repair_attempts: Optional[int] = None,
    ) -> HermesTaskResponse:
        """Execute the end-to-end Gemini Dev + GLM/DeepSeek Review closed loop."""
        target_repo = self.validate_repo_root(repo_root)
        from orchestrator.gemini_closed_loop import GeminiClosedLoopOrchestrator

        loop = GeminiClosedLoopOrchestrator(
            repo_root=target_repo,
            session=self.session,
            policy=self.policy,
            reviewer_choice=reviewer_choice,
            max_repair_attempts=max_repair_attempts,
        )
        result = await loop.run(
            requirement=requirement,
            title=title,
            mock_mode=mock_mode,
            custom_test_cmd=custom_test_cmd,
            test_paths=test_paths,
        )
        with loop._provide_session(mock_mode=mock_mode) as s:
            task = s.exec(select(Task).where(Task.task_id == result.task_id)).first()
            if task:
                return self._build_task_response(task, deduplicated=False)
        return HermesTaskResponse(
            task_id=result.task_id,
            status=result.state,
            state=result.state,
            title=title or requirement[:40],
            requirement=requirement,
            primary_model="gemini-3.8-flash-high",
            result_summary=result.result_summary,
            repair_attempts=result.repair_attempts,
            deduplicated=False,
        )

