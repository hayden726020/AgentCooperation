"""Single-Flash orchestration with budget reservations and verified delivery."""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from sqlmodel import Session, select

from agents.base import AgentResult
from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from agents.gemini_reviewer import GeminiReviewer
from config.settings import settings
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

    @staticmethod
    def _project_context(root: Path, limit=24000):
        """Bound source context; do not follow links or read credential files."""
        root = root.resolve(strict=True)
        def safe_path(path):
            try:
                resolved = path.resolve(strict=True)
                return resolved.is_relative_to(root) and resolved == path.absolute()
            except (OSError, RuntimeError):
                return False
        excluded = {".git", ".worktrees", "references", "data", "node_modules", ".venv",
                    "venv", "__pycache__", ".pytest_cache", ".pytest_temp", "dist", "build"}
        extensions = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md", ".json", ".toml"}
        parts = []
        remaining = limit
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith(".")
                             and safe_path(Path(directory) / d))
            for name in sorted(files):
                file = Path(directory) / name
                if not safe_path(file) or file.suffix not in extensions or name.startswith("."):
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
                if remaining <= 0:
                    return "".join(parts)
        return "".join(parts)

    async def _call_primary(self, task_id, agent, step, prompt, worktree, mock_mode):
        context = {"worktree_path": worktree, "project_context": self._project_context(worktree),
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
        if not result.metadata.get("files_written"):
            raise RuntimeError(f"{step} produced no applicable files")
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
        for name in generated_files:
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
                       mock_mode: bool = False, custom_test_cmd: Optional[str] = None) -> Task:
        if not requirement.strip():
            raise ValueError("Task requirement must not be empty")
        if not mock_mode:
            can_start, message = self.budget_guard.check_can_start_new_task()
            if not can_start:
                raise BudgetLimitError(message)
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        model_name = chosen_model or self.policy.primary_agent
        with self._session() as session:
            session.add(Task(task_id=task_id, title=title or requirement[:40], requirement=requirement,
                             primary_model=model_name, branch_name=f"codex/task_{task_id}", state="INIT"))
            session.commit()
        fsm = TaskLifecycleFSM(max_repair_attempts=self.policy.repair.max_normal_attempts)
        generated_files = set()
        last_review = None
        fallback_used = False

        try:
            primary = SingleEngineSlotEnforcer(self.policy).get_primary_agent(model_name, mock_mode=mock_mode)
            reviewer = GeminiReviewer(mock_mode=mock_mode)
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
                    review_result = await reviewer.execute("Review requirements, code and actual test evidence.", context={
                        "worktree_path": worktree, "git_diff": diff, "test_summary": test_result.raw_output,
                        "requirement": requirement,
                    })
                    self._log_step(task_id, "Gemini Review", "REVIEWER", "Gemini-CLI", review_result)
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
                        suffix = " (simulation; not a live delivery)" if mock_mode else ""
                        return self._finish(task_id, fsm, "Tests and independent review passed" + suffix)
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
                if fsm.state == "REPAIRING":
                    repair = await self._call_primary(task_id, primary, f"Repair #{fsm.repair_attempts}",
                        f"Requirement:\n{requirement}\nPlan:\n{plan.content}\n"
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
                    task = session.exec(select(Task).where(Task.task_id == task_id)).one()
                    expert_allowed = expert_available and task.codex_calls < self.policy.codex.max_calls_per_task
                    if expert_allowed and not quota_exhausted:
                        task.codex_calls += 1
                        session.add(task)
                        session.commit()
                if expert_allowed and not quota_exhausted:
                    try:
                        advice = await expert.execute("Diagnose the issue and provide a verifiable repair plan.", context={
                            "worktree_path": worktree,
                            "issue_context": f"Requirement: {requirement}\nSeverity: {severity}\n"
                                             f"Evidence: {issue}\nTests: {test_result.raw_output}\nDiff:\n{diff}",
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
                        issue_context=f"Requirement:\n{requirement}\nPlan:\n{plan.content}",
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
                    f"Requirement:\n{requirement}\nPlan:\n{plan.content}\n{patch_prompt}\nTests:\n{test_result.raw_output}",
                    worktree, mock_mode)
                generated_files.update(patch.metadata.get("files_written", []))
                checkpoint = await self.worktree_mgr.commit_checkpoint(worktree, "fix: expert or fallback repair")
                self.codex_guard.update_pending_evidence(
                    task_id, checkpoint, await self.worktree_mgr.get_diff(worktree, base_ref=base_ref),
                    "Repair committed; validation pending"
                )
                fsm.to_testing()

        except BudgetLimitError as error:
            fsm.to_budget_stopped()
            self._log_step(task_id, "Budget Stop", "SYSTEM", "Orchestrator", error=str(error))
            return self._finish(task_id, fsm, str(error))
        except asyncio.CancelledError:
            fsm.to_interrupted()
            self._finish(task_id, fsm, "Execution interrupted; automatic delivery withheld")
            raise
        except Exception as error:
            fsm.to_failed()
            self._log_step(task_id, "Fatal Exception", "SYSTEM", "Orchestrator", error=str(error))
            return self._finish(task_id, fsm, f"Execution failed: {error}")
