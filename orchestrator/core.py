"""Single-Flash orchestration with budget reservations and verified delivery."""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from typing import Literal, Optional

from sqlmodel import Session, select

from agents.base import AgentResult
from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from agents.gemini_reviewer import GeminiReviewer
from agents.codex_reviewer import CodexReviewer
from config.settings import settings
from execution.security import contains_sensitive_files, redact_and_truncate_evidence
from execution.test_runner import TestRunner
from execution.worktree import WorktreeManager
from orchestrator.budget_guard import BudgetGuard, BudgetLimitError
from orchestrator.codex_guard import CodexQuotaGuard
from orchestrator.engine_selector import SingleEngineSlotEnforcer
from orchestrator.fsm import TaskLifecycleFSM
from storage.db import init_db, provide_session
from storage.models import ExecutionLog, Task


class OrchestratorCore:
    def __init__(self, repo_root: Optional[Path] = None, session: Optional[Session] = None):
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.session = session
        self.policy = settings.load_policy()
        init_db(session.get_bind() if session is not None else None)
        self.budget_guard = BudgetGuard(self.policy.budget, session)
        self.codex_guard = CodexQuotaGuard(self.policy.codex, session)
        self.worktree_mgr = WorktreeManager(self.repo_root, self.policy.execution.git_worktree_base)

    def _session(self):
        return provide_session(self.session)

    def _get_reviewer(self, mock_mode=False):
        if self.policy.reviewer_agent == "gemini_cli":
            return GeminiReviewer(mock_mode=mock_mode)
        if self.policy.reviewer_agent == "codex_cli":
            return CodexReviewer(mock_mode=mock_mode)
        if self.policy.reviewer_agent == "antigravity_cli":
            from agents.antigravity_reviewer import AntigravityReviewer
            return AntigravityReviewer(mock_mode=mock_mode)
        raise ValueError(f"Unsupported independent reviewer: {self.policy.reviewer_agent}")

    def _defer_codex_review(self, task_id, fsm, checkpoint, diff, tests, worktree,
                           requirement, reason):
        self.codex_guard.handle_quota_exhausted(
            task_id=task_id, commit_sha=checkpoint, severity="P1",
            issue_description="Independent Codex review deferred: subscription quota exhausted",
            git_diff_summary=diff, worktree_path=str(worktree), test_summary=tests,
            issue_context=f"Original requirement:\n{requirement}", reason=reason,
        )
        self._log_step(task_id, "Codex-CLI Review", "REVIEWER", "Codex-CLI",
                       status="DEFERRED", error=reason)
        fsm.to_waiting_review()
        return self._finish(task_id, fsm, "Tests passed; independent Codex review queued until quota recovers")

    def _log_step(self, task_id, step_name, role, model, result=None, *, status=None, error=None):
        result = result or AgentResult(success=not error, content="", error=error)
        with self._session() as session:
            session.add(ExecutionLog(
                task_id=task_id, step_name=step_name, agent_role=role, agent_model=model,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                cost_cny=result.cost_cny,
                status=status or ("SUCCESS" if result.success else "FAILED"),
                output_snippet=result.content[:500], error_snippet=(error or result.error or "")[:500],
            ))
            session.commit()

    def _save_state(self, task_id, fsm):
        with self._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).one()
            task.state = fsm.state
            task.repair_attempts = fsm.repair_attempts
            task.updated_at = datetime.now(timezone.utc)
            session.add(task)
            session.commit()

    def _finish(self, task_id, fsm, summary):
        self._save_state(task_id, fsm)
        with self._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).one()
            task.result_summary = summary
            if fsm.state in {"SUCCESS", "FAILED"}:
                task.completed_at = datetime.now(timezone.utc)
            session.add(task)
            session.commit()
            session.refresh(task)
            session.expunge(task)
            return task

    def _handle_execution_exception(
        self,
        task_id: str,
        fsm: TaskLifecycleFSM,
        error: BaseException,
        probe_summary: Optional[str] = None,
    ) -> Optional[Task]:
        if isinstance(error, BudgetLimitError):
            fsm.to_budget_stopped()
            self._log_step(task_id, "Budget Stop", "SYSTEM", "Orchestrator", error=str(error))
            return self._finish(task_id, fsm, str(error))
        if isinstance(error, asyncio.CancelledError):
            fsm.to_interrupted()
            self._finish(task_id, fsm, "Execution interrupted; automatic delivery withheld")
            raise
        fsm.to_failed()
        err_msg = f"Execution failed: {error}"
        if probe_summary:
            err_msg = f"{err_msg} | Worktree probe: {probe_summary}"
        self._log_step(task_id, "Fatal Exception", "SYSTEM", "Orchestrator", error=err_msg)
        return self._finish(task_id, fsm, err_msg)

    @staticmethod
    def _project_context(root: Path, limit=24000, target_modules: Optional[list[str]] = None):
        """Prioritize current briefs, architecture maps and balanced source within a fixed character budget."""
        root = root.resolve(strict=True)

        def safe_path(path):
            try:
                resolved = path.resolve(strict=True)
                return resolved.is_relative_to(root) and resolved == path.absolute()
            except (OSError, RuntimeError):
                return False

        excluded = {".git", ".worktrees", "references", "data", "node_modules", ".venv",
                    "venv", "__pycache__", ".pytest_cache", ".pytest_temp", "dist", "build"}
        extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md",
                      ".json", ".toml", ".yaml", ".yml"}
        priority = [root / name for name in ("AGENTS.md", "docs/CONTEXT.md", "README.md")]
        arch_map = root / ".planning" / "codebase" / "architecture.md"
        if arch_map.is_file() and safe_path(arch_map):
            priority.append(arch_map)

        def source_files(markdown):
            collected_by_dir: dict[str, list[Path]] = {}
            for directory, dirs, files in os.walk(root, followlinks=False):
                base = Path(directory)
                dirs[:] = sorted(
                    d for d in dirs
                    if d not in excluded and not d.startswith(".")
                    and safe_path(base / d)
                    and not os.path.lexists(base / d / ".git")
                    and (base / d).relative_to(root).as_posix() != "docs/archive"
                )
                for name in sorted(files):
                    file = base / name
                    if file not in priority and (file.suffix == ".md") == markdown:
                        top_dir = file.relative_to(root).parts[0] if len(file.relative_to(root).parts) > 1 else "."
                        collected_by_dir.setdefault(top_dir, []).append(file)

            target_set = set(target_modules or [])
            if target_set:
                for top_dir in sorted(collected_by_dir.keys()):
                    if top_dir in target_set:
                        for f in collected_by_dir[top_dir]:
                            yield f
                for top_dir in sorted(collected_by_dir.keys()):
                    if top_dir not in target_set:
                        for f in collected_by_dir[top_dir]:
                            yield f
            else:
                if "." in collected_by_dir:
                    for f in collected_by_dir["."]:
                        yield f
                other_dirs = [d for d in sorted(collected_by_dir.keys()) if d != "."]
                max_len = max((len(collected_by_dir[d]) for d in other_dirs), default=0)
                for idx in range(max_len):
                    for d in other_dirs:
                        if idx < len(collected_by_dir[d]):
                            yield collected_by_dir[d][idx]

        parts = []
        remaining = limit
        for file in chain(priority, source_files(False), source_files(True)):
            if remaining <= 0:
                break
            name = file.name
            if (not safe_path(file) or name.startswith(".")
                    or (file.suffix not in extensions and name != "requirements.txt")):
                continue
            if any(os.path.lexists(parent / ".git") for parent in file.parents
                   if parent != root and parent.is_relative_to(root)):
                continue
            if any(word in name.lower() for word in ("secret", "credential", "token", "auth", "key")):
                continue
            try:
                if file.stat().st_size > 128000:
                    continue
                text = file.read_text(encoding="utf-8")[:min(8000, remaining)]
            except (OSError, UnicodeError):
                continue
            part = f"\n=== {file.relative_to(root).as_posix()} ===\n{text}\n"
            parts.append(part[:remaining])
            remaining -= len(parts[-1])
        return "".join(parts)

    async def _call_primary(self, task_id, agent, step, prompt, worktree, mock_mode, target_modules: Optional[list[str]] = None):
        context = {"worktree_path": worktree, "project_context": self._project_context(worktree, target_modules=target_modules),
                   "max_output_tokens": self.policy.execution.max_output_tokens}
        estimate = agent.estimate_max_cost(prompt, context)
        reservation = self.budget_guard.reserve_call(task_id, estimate, simulated=mock_mode)
        context["reserved_cost_cny"] = estimate
        try:
            result = await agent.execute(prompt, context=context)
        except BaseException:
            self.budget_guard.settle_call(reservation, 0, 0, cost_cny=None)
            raise
        self.budget_guard.settle_call(
            reservation, result.prompt_tokens, result.completion_tokens,
            cost_cny=None if result.metadata.get("usage_missing") else result.cost_cny,
        )
        self._log_step(task_id, step, "PRIMARY", agent.model_name, result)
        if not result.success:
            raise RuntimeError(f"{step} failed: {result.error or 'model returned failure'}")
        if not mock_mode and result.metadata.get("mock"):
            raise RuntimeError("A live task cannot accept a simulated model result")
        files_written = result.metadata.get("files_written", [])
        if not files_written:
            raise RuntimeError(f"{step} produced no applicable files")
        has_sens, sens_path = contains_sensitive_files(files_written)
        if has_sens:
            raise ValueError(f"Task attempted to write to sensitive protected path: {sens_path}")
        return result

    @staticmethod
    def _test_paths(worktree, generated_files):
        paths = set()
        # Run existing target regressions as well as newly generated tests.
        existing = worktree / "tests"
        if existing.is_dir() and not existing.is_symlink():
            paths.add("tests")
        for path in (*worktree.glob("test_*.py"), *worktree.glob("*_test.py")):
            if path.is_file() and not path.is_symlink():
                paths.add(path.name)
        for name in (generated_files or []):
            path = Path(name)
            path = path if path.is_absolute() else worktree / path
            try:
                relative = path.resolve().relative_to(worktree.resolve())
            except ValueError:
                continue
            if path.exists() and path.suffix == ".py" and (
                path.name.startswith("test_") or path.name.endswith("_test.py")
            ):
                if not ("tests" in paths and relative.parts[0] == "tests"):
                    paths.add(relative.as_posix())
        return sorted(paths)

    async def run_task(self, requirement: str, title: Optional[str] = None,
                       chosen_model: Optional[Literal["glm", "deepseek"]] = None,
                       mock_mode: bool = False, custom_test_cmd: Optional[str] = None,
                       task_id: Optional[str] = None) -> Task:
        if not requirement.strip():
            raise ValueError("Task requirement must not be empty")
        if not mock_mode:
            can_start, message = self.budget_guard.check_can_start_new_task()
            if not can_start:
                raise BudgetLimitError(message)
        task_id = task_id or f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        model_name = chosen_model or self.policy.primary_agent
        with self._session() as session:
            existing = session.exec(select(Task).where(Task.task_id == task_id)).first()
            if not existing:
                session.add(Task(task_id=task_id, title=title or requirement[:40], requirement=requirement,
                                 primary_model=model_name, branch_name=f"codex/task_{task_id}", state="INIT"))
                session.commit()
        fsm = TaskLifecycleFSM(max_repair_attempts=self.policy.repair.max_normal_attempts)
        generated_files = set()
        last_review = None
        fallback_used = False

        try:
            primary = SingleEngineSlotEnforcer(self.policy).get_primary_agent(model_name, mock_mode=mock_mode)
            reviewer = self._get_reviewer(mock_mode=mock_mode)
            expert = CodexExpert(mock_mode=mock_mode)
            worktree = (await self.worktree_mgr.create_worktree(task_id)).resolve()
            base_ref = await self.worktree_mgr.get_base_ref(worktree)
            checkpoint = base_ref
            with self._session() as session:
                task = session.exec(select(Task).where(Task.task_id == task_id)).one()
                task.worktree_path = str(worktree)
                session.add(task)
                session.commit()

            fsm.to_planning()
            self._save_state(task_id, fsm)
            plan = await self._call_primary(task_id, primary, "Planning",
                f"Plan this task and write TASK.md and SPEC.json with objective acceptance tests:\n{requirement}",
                worktree, mock_mode)
            generated_files.update(plan.metadata.get("files_written", []))
            fsm.to_developing()
            self._save_state(task_id, fsm)
            development = await self._call_primary(task_id, primary, "Developing",
                f"Requirement:\n{requirement}\n\nImplementation plan:\n{plan.content}\n\n"
                "Implement runnable files and tests that exercise the stated requirements. Do not weaken tests.",
                worktree, mock_mode)
            generated_files.update(development.metadata.get("files_written", []))
            checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, "feat: generated implementation and tests")
            fsm.to_testing()

            sim_suffix = " (simulation; not a live delivery)" if mock_mode else ""
            return await self._run_test_review_repair_loop(
                task_id=task_id,
                fsm=fsm,
                requirement=requirement,
                primary=primary,
                reviewer=reviewer,
                expert=expert,
                worktree=worktree,
                base_ref=base_ref,
                checkpoint=checkpoint,
                generated_files=generated_files,
                mock_mode=mock_mode,
                custom_test_cmd=custom_test_cmd,
                plan_content=plan.content,
                success_suffix=sim_suffix,
            )
        except (BudgetLimitError, asyncio.CancelledError, Exception) as error:
            probe_summary = None
            try:
                if "worktree" in locals() and worktree:
                    probe = await self.worktree_mgr.probe_worktree_status(worktree, base_ref=base_ref if "base_ref" in locals() else None)
                    probe_summary = probe.summary
            except Exception:
                pass
            return self._handle_execution_exception(task_id, fsm, error, probe_summary=probe_summary)

    async def _run_test_review_repair_loop(
        self,
        task_id: str,
        fsm: TaskLifecycleFSM,
        requirement: str,
        primary: Any,
        reviewer: Any,
        expert: CodexExpert,
        worktree: Path,
        base_ref: str,
        checkpoint: str,
        generated_files: set[str],
        mock_mode: bool,
        custom_test_cmd: Optional[str] = None,
        plan_content: Optional[str] = None,
        success_suffix: str = "",
    ) -> Task:
        last_review = None
        fallback_used = False
        try:
            while True:
                self._save_state(task_id, fsm)
                runner = TestRunner(worktree, custom_test_cmd=custom_test_cmd,
                                    test_paths=self._test_paths(worktree, generated_files) or None)
                test_result = await runner.run(timeout=self.policy.execution.timeout_seconds)
                current_diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
                self.codex_guard.update_pending_evidence(
                    task_id, checkpoint, current_diff, test_result.raw_output
                )
                fsm.tests_passed = test_result.passed
                fsm.review_approved = False
                self._log_step(task_id, f"Testing (attempt {fsm.repair_attempts})", "TEST_RUNNER", "LocalRunner",
                               AgentResult(test_result.passed, test_result.raw_output,
                                           error=None if test_result.passed else test_result.summary))
                issue = test_result.raw_output
                severity = "P1"

                if test_result.passed:
                    fsm.to_reviewing()
                    self._save_state(task_id, fsm)
                    diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
                    uses_live_codex = not mock_mode and self.policy.reviewer_agent == "codex_cli"
                    if uses_live_codex:
                        self.codex_guard._sync_state_from_db()
                        if self.codex_guard.state == "EXHAUSTED":
                            return self._defer_codex_review(
                                task_id, fsm, checkpoint, diff, test_result.raw_output, worktree,
                                requirement, "Persisted Codex subscription quota exhausted",
                            )
                    try:
                        review_result = await reviewer.execute("Review requirements, code and actual test evidence.", context={
                            "worktree_path": worktree, "git_diff": diff, "test_summary": test_result.raw_output,
                            "requirement": requirement,
                            "expected_commit_sha": checkpoint,
                        })
                    except CodexQuotaExhaustedError as quota_error:
                        if not uses_live_codex:
                            raise
                        return self._defer_codex_review(
                            task_id, fsm, checkpoint, diff, test_result.raw_output, worktree,
                            requirement, str(quota_error),
                        )
                    reviewer_model = getattr(reviewer, "model_name", self.policy.reviewer_agent)
                    actual_review_model = review_result.metadata.get("review_model") or reviewer_model
                    self._log_step(task_id, f"{reviewer_model} Review", "REVIEWER", actual_review_model, review_result)
                    if not review_result.success:
                        raise RuntimeError(f"Independent review failed: {review_result.error}")
                    last_review = review_result.metadata.get("review")
                    if (not isinstance(last_review, dict) or type(last_review.get("approved")) is not bool
                            or last_review.get("severity") not in {"P0", "P1", "P2", "P3"}):
                        raise RuntimeError("Independent review did not provide a valid verdict")
                    severity = last_review["severity"]
                    issue = review_result.content
                    if last_review["approved"] and severity not in {"P0", "P1"}:
                        self.codex_guard.update_pending_evidence(
                            task_id, checkpoint, diff, test_result.raw_output, json.dumps(last_review)
                        )
                        fsm.review_approved = True
                        fsm.to_success()
                        return self._finish(task_id, fsm, f"Tests and independent review passed{success_suffix}")
                    if severity == "P0":
                        fsm.to_escalating_from_review()
                    elif fsm.can_repair() and severity not in {"P0", "P1"} and not fallback_used:
                        fsm.to_repairing_from_review()
                    else:
                        fsm.to_escalating_from_review()
                elif fsm.can_repair() and not fallback_used:
                    fsm.to_repairing()
                else:
                    fsm.to_escalating_from_test()

                self._save_state(task_id, fsm)
                plan_section = f"Plan:\n{plan_content}\n" if plan_content else ""
                if fsm.state == "REPAIRING":
                    repair = await self._call_primary(task_id, primary, f"Repair #{fsm.repair_attempts}",
                        f"Requirement:\n{requirement}\n{plan_section}"
                        f"Test/review rejection evidence:\n{issue}\nRepair the implementation; preserve acceptance tests.",
                        worktree, mock_mode)
                    generated_files.update(repair.metadata.get("files_written", []))
                    checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, f"fix: repair #{fsm.repair_attempts}")
                    fsm.to_testing()
                    continue

                diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
                advice = None
                expert_available = self.codex_guard.is_available()
                quota_exhausted = self.policy.codex.enabled and self.codex_guard.state == "EXHAUSTED"
                with self._session() as session:
                    task_rec = session.exec(select(Task).where(Task.task_id == task_id)).one()
                    expert_allowed = expert_available and task_rec.codex_calls < self.policy.codex.max_calls_per_task
                    if expert_allowed and not quota_exhausted:
                        task_rec.codex_calls += 1
                        session.add(task_rec)
                        session.commit()
                if expert_allowed and not quota_exhausted:
                    try:
                        advice = await expert.execute("Diagnose the issue and provide a verifiable repair plan.", context={
                            "worktree_path": worktree,
                            "issue_context": f"Requirement: {requirement}\nSeverity: {severity}\n"
                                             f"Evidence: {redact_and_truncate_evidence(issue)}\n"
                                             f"Tests: {redact_and_truncate_evidence(test_result.raw_output)}\n"
                                             f"Diff:\n{redact_and_truncate_evidence(diff)}",
                        })
                        self._log_step(task_id, "Codex Escalation", "EXPERT", "Codex-CLI", advice)
                    except CodexQuotaExhaustedError:
                        quota_exhausted = True

                if quota_exhausted:
                    self.codex_guard.handle_quota_exhausted(
                        task_id=task_id, commit_sha=checkpoint, severity=severity,
                        issue_description=issue, git_diff_summary=diff,
                        gemini_review_json=json.dumps(last_review) if last_review else None,
                        worktree_path=str(worktree), test_summary=test_result.raw_output,
                        issue_context=f"Requirement:\n{requirement}\n{plan_section}".rstrip(),
                        reason="Confirmed Codex usage limit or persisted exhausted state",
                    )

                if severity == "P0":
                    fsm.to_waiting_approval()
                    return self._finish(task_id, fsm, "P0 risk requires authorized human approval; no automatic expert patch applied")
                if not expert_allowed and not quota_exhausted:
                    fsm.to_waiting_review()
                    return self._finish(task_id, fsm, "Expert review disabled or task call limit reached")
                if advice is not None and not advice.success:
                    raise RuntimeError(f"Expert review failed: {advice.error}")
                if quota_exhausted:
                    if fallback_used:
                        raise RuntimeError("Fallback did not pass tests and independent review; audit remains queued")
                    fallback_used = True
                    fsm.to_codex_backlogged()
                    fsm.to_repairing_from_backlog()
                    patch_prompt = f"Codex is unavailable. Independently diagnose and repair this P1 issue:\n{issue}\n"
                else:
                    fsm.to_repairing_from_codex()
                    patch_prompt = f"Expert advice:\n{advice.content}\nApply the advised implementation repair.\n"
                self._save_state(task_id, fsm)
                patch = await self._call_primary(task_id, primary, "Expert/Fallback Repair",
                    f"Requirement:\n{requirement}\n{plan_section}{patch_prompt}\nTests:\n{test_result.raw_output}",
                    worktree, mock_mode)
                generated_files.update(patch.metadata.get("files_written", []))
                checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, "fix: expert or fallback repair")
                self.codex_guard.update_pending_evidence(
                    task_id, checkpoint, await self.worktree_mgr.get_diff(worktree, base_ref=base_ref),
                    "Repair committed; validation pending"
                )
                fsm.to_testing()
        except (BudgetLimitError, asyncio.CancelledError, Exception) as error:
            return self._handle_execution_exception(task_id, fsm, error)

    async def resume_task_after_approval(
        self,
        task_id: str,
        approved: bool,
        notes: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
    ) -> Task:
        """Resume execution of a task currently held in WAITING_APPROVAL.

        Human approval authorizes subsequent processing; the task must still pass
        necessary repair, testing, and independent review before reaching SUCCESS.
        """
        with self._session() as session:
            task = session.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.state != "WAITING_APPROVAL":
                raise ValueError(f"Task '{task_id}' is in state '{task.state}', expected 'WAITING_APPROVAL'.")

        decision_note = notes or "No notes provided"
        if not approved:
            fsm = TaskLifecycleFSM(initial_state="WAITING_APPROVAL")
            fsm.to_failed()
            self._log_step(
                task_id,
                "Human Approval Decision",
                "SYSTEM",
                "HumanOperator",
                AgentResult(False, f"Rejected by human operator: {decision_note}", error=f"Human rejection: {decision_note}"),
            )
            return self._finish(task_id, fsm, f"Rejected by human operator: {decision_note}")

        fsm = TaskLifecycleFSM(
            max_repair_attempts=self.policy.repair.max_normal_attempts,
            initial_state="WAITING_APPROVAL",
        )
        fsm.repair_attempts = task.repair_attempts
        self._log_step(
            task_id,
            "Human Approval Decision",
            "SYSTEM",
            "HumanOperator",
            AgentResult(True, f"Approved by human operator: {decision_note}"),
        )
        fsm.to_repairing_from_approval()
        self._save_state(task_id, fsm)

        model_name = task.primary_model or self.policy.primary_agent
        primary = SingleEngineSlotEnforcer(self.policy).get_primary_agent(model_name, mock_mode=mock_mode)
        reviewer = self._get_reviewer(mock_mode=mock_mode)
        expert = CodexExpert(mock_mode=mock_mode)
        worktree_str = task.worktree_path
        if not worktree_str or not Path(worktree_str).exists():
            worktree = (await self.worktree_mgr.create_worktree(task_id)).resolve()
            with self._session() as session:
                task_db = session.exec(select(Task).where(Task.task_id == task_id)).first()
                if task_db:
                    task_db.worktree_path = str(worktree)
                    session.add(task_db)
                    session.commit()
        else:
            worktree = Path(worktree_str).resolve()
        base_ref = await self.worktree_mgr.get_base_ref(worktree)
        checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, "chore: checkpoint before approved repair")
        requirement = task.requirement
        generated_files = set(self._test_paths(worktree, []) or [])

        try:
            # Execute repair with Primary Agent incorporating operator instructions
            repair_prompt = (
                f"Requirement:\n{requirement}\n\n"
                f"Human Operator Approval:\n{decision_note}\n\n"
                "Apply the authorized implementation repair and ensure all tests pass. Do not weaken tests."
            )
            repair_res = await self._call_primary(
                task_id, primary, f"Approved Repair #{fsm.repair_attempts}", repair_prompt, worktree, mock_mode
            )
            generated_files.update(repair_res.metadata.get("files_written", []))
            checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, f"fix: human approved repair #{fsm.repair_attempts}")
            fsm.to_testing()

            sim_suffix = " (simulation; not a live delivery)" if mock_mode else ""
            return await self._run_test_review_repair_loop(
                task_id=task_id,
                fsm=fsm,
                requirement=requirement,
                primary=primary,
                reviewer=reviewer,
                expert=expert,
                worktree=worktree,
                base_ref=base_ref,
                checkpoint=checkpoint,
                generated_files=generated_files,
                mock_mode=mock_mode,
                custom_test_cmd=custom_test_cmd,
                plan_content=None,
                success_suffix=" after human approval" + sim_suffix,
            )
        except (BudgetLimitError, asyncio.CancelledError, Exception) as error:
            probe_summary = None
            try:
                if "worktree" in locals() and worktree:
                    probe = await self.worktree_mgr.probe_worktree_status(worktree, base_ref=base_ref if "base_ref" in locals() else None)
                    probe_summary = probe.summary
            except Exception:
                pass
            return self._handle_execution_exception(task_id, fsm, error, probe_summary=probe_summary)
