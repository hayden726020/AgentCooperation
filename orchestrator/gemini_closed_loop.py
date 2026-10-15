"""Closed-loop orchestrator: Gemini development with GLM or DeepSeek independent review.

Coordinates:
1. Task Handoff: Dispatches user requirement into an isolated Git worktree for Gemini development.
2. Artifact Transfer: Collects commit SHA, Git diff, and test evidence as read-only inputs for review.
3. Review Verdict Callback: Independent reviewer (GLM / DeepSeek) audits evidence and returns structured verdict.
4. Failure Retry Orchestration: Retries repairs with rejection evidence up to bounded attempts, blocking on P0 risks.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Literal, Optional
import uuid

from sqlmodel import Session, select

from agents.base import AgentResult
from agents.flash_reviewer import FlashReviewer
from agents.gemini_dev_agent import GeminiDevAgent
from config.settings import PROJECT_ROOT, PolicyConfig, settings
from execution.test_runner import TestRunner
from execution.worktree import WorktreeManager
from orchestrator.budget_guard import BudgetGuard, BudgetLimitError
from storage.db import get_engine, get_session, init_db, provide_session
from storage.models import ExecutionLog, Task, utc_now
import re

logger = logging.getLogger(__name__)

_SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|token|password|bearer|authorization)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{8,})['\"]?"), r"\1=[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"Bearer\s+ya29\.[A-Za-z0-9_\-]+", re.IGNORECASE), "Bearer [REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"ya29\.[A-Za-z0-9_\-]+"), "[REDACTED_OAUTH_TOKEN]"),
]


def redact_and_truncate_evidence(content: str, max_chars: int = 60000) -> str:
    if not content:
        return ""
    text = content
    for pattern, repl in _SENSITIVE_PATTERNS:
        text = pattern.sub(repl, text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [TRUNCATED {len(text) - max_chars} characters for review safety]"
    return text


def validate_custom_test_cmd(cmd: str | None) -> None:
    if not cmd:
        return
    forbidden_patterns = [r"[;&|`$]", r"\b(rm|del|rmdir|shutil|curl|wget|bash|sh|cmd|powershell|pwsh)\b"]
    for pat in forbidden_patterns:
        if re.search(pat, cmd, re.IGNORECASE):
            raise ValueError(f"Custom test command contains disallowed patterns: {cmd}")
    allowed_prefixes = ("python -m pytest", "python.exe -m pytest", "pytest", "pytest.exe")
    if not any(cmd.strip().startswith(prefix) for prefix in allowed_prefixes):
        raise ValueError(f"Custom test command must start with approved test runner (python -m pytest / pytest): {cmd}")


@dataclass
class DevelopmentArtifacts:
    """Read-only development artifacts transferred to independent review."""

    task_id: str
    commit_sha: str
    base_ref: str
    git_diff: str
    test_passed: bool
    test_summary: str
    generated_files: list[str] = field(default_factory=list)
    repair_attempts: int = 0


@dataclass
class ReviewVerdict:
    """Structured callback verdict returned by the independent reviewer."""

    approved: bool
    severity: Literal["P0", "P1", "P2", "P3"]
    summary: str
    critical_issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reviewer_model: str = "glm-5.3-flashx"


@dataclass
class ClosedLoopResult:
    """Final output of the closed-loop task execution."""

    task_id: str
    success: bool
    state: str
    result_summary: str
    repair_attempts: int
    final_commit_sha: Optional[str] = None
    artifacts: Optional[DevelopmentArtifacts] = None
    verdict: Optional[ReviewVerdict] = None
    history: list[dict[str, Any]] = field(default_factory=list)


class GeminiClosedLoopOrchestrator:
    """Coordinates Gemini development and GLM/DeepSeek independent review in a verified loop."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        session: Optional[Session] = None,
        policy: Optional[PolicyConfig] = None,
        reviewer_choice: Literal["glm", "deepseek"] = "glm",
        max_repair_attempts: Optional[int] = None,
    ):
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.session = session
        self.policy = policy or settings.load_policy()
        init_db(session.get_bind() if session is not None else None)
        self.budget_guard = BudgetGuard(self.policy.budget, session)
        self.worktree_mgr = WorktreeManager(self.repo_root, self.policy.execution.git_worktree_base)
        self.reviewer_choice = reviewer_choice
        self.max_repair_attempts = (
            max_repair_attempts
            if max_repair_attempts is not None
            else getattr(self.policy.repair, "max_normal_attempts", 3)
        )

    def _provide_session(self, mock_mode: bool = False):
        if self.session is not None:
            return provide_session(self.session)
        if mock_mode:
            mock_db_path = PROJECT_ROOT / "data/mock_agent_orchestrator.db"
            mock_db_path.parent.mkdir(parents=True, exist_ok=True)
            mock_engine = get_engine(str(mock_db_path))
            init_db(mock_engine)
            return get_session(mock_engine)
        return provide_session(self.session)

    def _log_step(
        self,
        task_id: str,
        step_name: str,
        agent_role: str,
        agent_model: str,
        status: str,
        output_snippet: str = "",
        error_snippet: str = "",
        cost_cny: Optional[float] = None,
        mock_mode: bool = False,
    ):
        with self._provide_session(mock_mode) as s:
            s.add(
                ExecutionLog(
                    task_id=task_id,
                    step_name=step_name,
                    agent_role=agent_role,
                    agent_model=agent_model,
                    status=status,
                    output_snippet=(output_snippet or "")[:500],
                    error_snippet=(error_snippet or "")[:500],
                    cost_cny=cost_cny,
                )
            )
            s.commit()

    def _get_budget_guard(self, mock_mode: bool = False) -> BudgetGuard:
        if self.session is not None:
            return BudgetGuard(self.policy.budget, self.session)
        if mock_mode:
            mock_db_path = PROJECT_ROOT / "data/mock_agent_orchestrator.db"
            mock_db_path.parent.mkdir(parents=True, exist_ok=True)
            mock_engine = get_engine(str(mock_db_path))
            init_db(mock_engine)
            return BudgetGuard(self.policy.budget, Session(mock_engine))
        return self.budget_guard

    def _save_task_state(
        self,
        task_id: str,
        state: str,
        repair_attempts: int = 0,
        result_summary: Optional[str] = None,
        mock_mode: bool = False,
        commit_sha: Optional[str] = None,
        reviewer_model: Optional[str] = None,
        test_passed: Optional[bool] = None,
        worktree_path: Optional[str] = None,
    ):
        with self._provide_session(mock_mode) as s:
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            if task:
                task.state = state
                task.repair_attempts = repair_attempts
                task.updated_at = utc_now()
                if result_summary is not None:
                    task.result_summary = result_summary
                if commit_sha is not None:
                    task.commit_sha = commit_sha
                if reviewer_model is not None:
                    task.reviewer_model = reviewer_model
                if test_passed is not None:
                    task.test_passed = test_passed
                if worktree_path is not None:
                    task.worktree_path = worktree_path
                if state in ("SUCCESS", "FAILED", "WAITING_APPROVAL"):
                    task.completed_at = utc_now()
                s.add(task)
                s.commit()

    @staticmethod
    def _test_paths(worktree: Path, generated_files: list[str]) -> list[str]:
        paths = set()
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
                paths.add(relative.as_posix())
        tests_dir = worktree / "tests"
        if tests_dir.is_dir():
            for path in (*tests_dir.glob("test_*.py"), *tests_dir.glob("*_test.py")):
                if path.is_file() and not path.is_symlink():
                    try:
                        relative = path.resolve().relative_to(worktree.resolve())
                        paths.add(relative.as_posix())
                    except ValueError:
                        pass
        for path in (*worktree.glob("test_*.py"), *worktree.glob("*_test.py")):
            if path.is_file() and not path.is_symlink():
                paths.add(path.name)
        return sorted(paths)

    async def run(
        self,
        requirement: str,
        title: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
        dev_agent: Optional[GeminiDevAgent] = None,
        reviewer_agent: Optional[FlashReviewer] = None,
        task_id: Optional[str] = None,
    ) -> ClosedLoopResult:

        if custom_test_cmd:
            validate_custom_test_cmd(custom_test_cmd)

        if not requirement or not requirement.strip():
            raise ValueError("Task requirement must not be empty")

        if not mock_mode:
            can_start, budget_msg = self.budget_guard.check_can_start_new_task()
            if not can_start:
                raise BudgetLimitError(budget_msg)

        task_id = task_id or f"task_gemini_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        title = title or requirement[:40]

        # Initialize Task record with mock_mode isolation
        with self._provide_session(mock_mode) as s:
            s.add(
                Task(
                    task_id=task_id,
                    title=title,
                    requirement=requirement,
                    primary_model="gemini-3.8-flash-high",
                    branch_name=f"gemini/{task_id}",
                    state="INIT",
                    mock_mode=mock_mode,
                )
            )
            s.commit()

        # Instantiate agents if not provided
        dev = dev_agent or GeminiDevAgent(mock_mode=mock_mode)
        reviewer = reviewer_agent or FlashReviewer(
            model_key=self.reviewer_choice,
            budget_cfg=self.policy.budget,
            mock_mode=mock_mode,
        )

        history: list[dict[str, Any]] = []
        repair_attempts = 0
        latest_artifacts: Optional[DevelopmentArtifacts] = None
        latest_verdict: Optional[ReviewVerdict] = None

        worktree = (await self.worktree_mgr.create_worktree(task_id)).resolve()
        base_ref = await self.worktree_mgr.get_base_ref(worktree)

        try:
            # -----------------------------------------------------------------
            # 1. Task Handoff & Initial Gemini Development
            # -----------------------------------------------------------------
            self._save_task_state(task_id, "DEVELOPING", repair_attempts=0, mock_mode=mock_mode, worktree_path=str(worktree))
            self._log_step(task_id, "Task Handoff to Gemini", "PRIMARY_DEVELOPER", dev.model_name, "IN_PROGRESS", mock_mode=mock_mode)

            dev_prompt = (
                f"You are the primary developer driving Gemini High. Implement this requirement:\n"
                f"{requirement}\n\n"
                "Instructions:\n"
                "1. Write the implementation and test files directly using `write_to_file`.\n"
                "2. When running tests with `run_command`, target ONLY the specific test file with `python -m pytest <test_file> -q`.\n"
                "   Never run the entire project test suite without arguments. Do not run commands in the background.\n"
                "3. Allowed tools: view_file, grep_search, write_to_file, replace_file_content, run_command."
            )
            dev_result = await dev.execute(dev_prompt, context={"worktree_path": worktree})
            if not dev_result.success:
                error_msg = f"Gemini initial development failed: {dev_result.error}"
                self._save_task_state(task_id, "FAILED", 0, error_msg, mock_mode=mock_mode)
                self._log_step(task_id, "Gemini Development", "PRIMARY_DEVELOPER", dev.model_name, "FAILED", error_snippet=error_msg, mock_mode=mock_mode)
                return ClosedLoopResult(
                    task_id=task_id,
                    success=False,
                    state="FAILED",
                    result_summary=error_msg,
                    repair_attempts=0,
                )

            # Gate 1: Check for successful native file writes
            files_written = dev_result.metadata.get("files_written", [])
            native_tools = dev_result.metadata.get("native_tool_steps", [])
            has_write_tool = any(
                item.get("name") in ("write_to_file", "replace_file_content") and item.get("succeeded")
                for item in native_tools
            ) or bool(files_written)
            if not mock_mode and not has_write_tool:
                error_msg = "Gemini completed without executing any successful native file modification tools"
                self._save_task_state(task_id, "FAILED", 0, error_msg, mock_mode=mock_mode)
                self._log_step(task_id, "Gate: Native Write Tool Verification", "SYSTEM", "Gate", "FAILED", error_snippet=error_msg, mock_mode=mock_mode)
                return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=0)

            # Commit initial development checkpoint
            checkpoint_sha = await self.worktree_mgr.commit_checkpoint(worktree, "feat: gemini initial development")
            diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)

            # Gate 2: Commit SHA changed and non-empty diff
            if not checkpoint_sha or checkpoint_sha == base_ref or not diff.strip():
                error_msg = "Gemini development produced no committed changes (empty diff or unchanged SHA)"
                self._save_task_state(task_id, "FAILED", 0, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                self._log_step(task_id, "Gate: Non-empty Commit Verification", "SYSTEM", "Gate", "FAILED", error_snippet=error_msg, mock_mode=mock_mode)
                return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=0)

            self._log_step(task_id, "Commit Gemini Implementation", "SYSTEM", "Git", "SUCCESS", output_snippet=checkpoint_sha, mock_mode=mock_mode)

            # Run test verification
            test_paths = self._test_paths(worktree, files_written)
            runner = TestRunner(worktree, custom_test_cmd=custom_test_cmd, test_paths=test_paths or None)
            test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

            # Gate 3: Verify clean working directory after test execution
            is_clean = await self.worktree_mgr.is_clean(worktree)
            if not is_clean:
                error_msg = "Working directory state modified during test execution (uncommitted changes detected)"
                self._save_task_state(task_id, "FAILED", 0, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha, test_passed=test_res.passed)
                return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=0)

            self._log_step(
                task_id,
                "Local Test Verification",
                "TEST_RUNNER",
                "LocalRunner",
                "SUCCESS" if test_res.passed else "FAILED",
                output_snippet=test_res.summary,
                mock_mode=mock_mode,
            )

            diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
            latest_artifacts = DevelopmentArtifacts(
                task_id=task_id,
                commit_sha=checkpoint_sha,
                base_ref=base_ref,
                git_diff=diff,
                test_passed=test_res.passed,
                test_summary=test_res.raw_output,
                generated_files=files_written,
                repair_attempts=repair_attempts,
            )

            # -----------------------------------------------------------------
            # 2. Main Loop: Artifact Transfer, Review Verdict, & Failure Retries
            # -----------------------------------------------------------------
            while True:
                # If tests did not pass, bypass reviewer and directly trigger repair with test failure evidence
                if not latest_artifacts.test_passed:
                    rejection_reason = f"Local tests failed before review:\n{latest_artifacts.test_summary}"
                    severity = "P1"
                else:
                    # ---------------------------------------------------------
                    # 2a. Artifact Transfer to Independent Reviewer
                    # ---------------------------------------------------------
                    self._save_task_state(task_id, "REVIEWING", repair_attempts=repair_attempts, mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha, test_passed=latest_artifacts.test_passed)
                    self._log_step(
                        task_id,
                        f"Artifact Transfer to {reviewer.model_key.upper()} Reviewer",
                        "REVIEWER",
                        reviewer.model_name,
                        "IN_PROGRESS",
                        output_snippet=f"Commit: {latest_artifacts.commit_sha[:8]}, Diff lines: {len(diff.splitlines())}",
                        mock_mode=mock_mode,
                    )

                    review_prompt = "Review requirements, code implementation, and actual test evidence."
                    review_context = {
                        "requirement": requirement,
                        "expected_commit_sha": latest_artifacts.commit_sha,
                        "git_diff": redact_and_truncate_evidence(latest_artifacts.git_diff),
                        "test_summary": redact_and_truncate_evidence(latest_artifacts.test_summary),
                        "worktree_path": str(worktree),
                    }
                    guard = self._get_budget_guard(mock_mode=mock_mode)
                    reservation_id = guard.reserve_call(task_id, 0.05, simulated=mock_mode)
                    try:
                        review_res = await reviewer.execute(review_prompt, context=review_context)
                    except BaseException:
                        guard.settle_call(reservation_id, 0, 0, cost_cny=None)
                        raise
                    actual_cost = review_res.cost_cny if (review_res.success and review_res.cost_cny is not None) else 0.0
                    guard.settle_call(
                        reservation_id,
                        review_res.prompt_tokens or 0,
                        review_res.completion_tokens or 0,
                        cost_cny=actual_cost,
                    )

                    if not review_res.success:
                        error_msg = f"Independent review failed: {review_res.error}"
                        self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha)
                        self._log_step(task_id, "Independent Review", "REVIEWER", reviewer.model_name, "FAILED", error_snippet=error_msg, mock_mode=mock_mode)
                        return ClosedLoopResult(
                            task_id=task_id,
                            success=False,
                            state="FAILED",
                            result_summary=error_msg,
                            repair_attempts=repair_attempts,
                            artifacts=latest_artifacts,
                        )

                    raw_verdict = review_res.metadata.get("review") or {}
                    latest_verdict = ReviewVerdict(
                        approved=bool(raw_verdict.get("approved")),
                        severity=raw_verdict.get("severity", "P2"),
                        summary=raw_verdict.get("summary", ""),
                        critical_issues=raw_verdict.get("critical_issues", []),
                        suggestions=raw_verdict.get("suggestions", []),
                        reviewer_model=reviewer.model_name,
                    )

                    self._log_step(
                        task_id,
                        f"{reviewer.model_key.upper()} Review Callback",
                        "REVIEWER",
                        reviewer.model_name,
                        "SUCCESS" if latest_verdict.approved else "REJECTED",
                        output_snippet=json.dumps(asdict(latest_verdict), ensure_ascii=False),
                        cost_cny=review_res.cost_cny,
                        mock_mode=mock_mode,
                    )

                    history.append({
                        "attempt": repair_attempts,
                        "commit_sha": latest_artifacts.commit_sha,
                        "test_passed": latest_artifacts.test_passed,
                        "review": asdict(latest_verdict),
                    })

                    # ---------------------------------------------------------
                    # 2b. Evaluate Review Decision
                    # ---------------------------------------------------------
                    if latest_verdict.approved and latest_verdict.severity not in ("P0", "P1"):
                        # SUCCESS: Approved and tests passed
                        success_summary = (
                            f"Verified closed-loop delivery: Gemini development and "
                            f"{reviewer.model_key.upper()} independent review passed after {repair_attempts} repairs."
                        )
                        self._save_task_state(
                            task_id, "SUCCESS", repair_attempts, success_summary,
                            mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha,
                            reviewer_model=reviewer.model_name, test_passed=latest_artifacts.test_passed,
                        )
                        return ClosedLoopResult(
                            task_id=task_id,
                            success=True,
                            state="SUCCESS",
                            result_summary=success_summary,
                            repair_attempts=repair_attempts,
                            final_commit_sha=latest_artifacts.commit_sha,
                            artifacts=latest_artifacts,
                            verdict=latest_verdict,
                            history=history,
                        )

                    # P0 Security Escalation: Halt immediately and wait for human authorization
                    if latest_verdict.severity == "P0":
                        p0_summary = (
                            f"P0 critical vulnerability or policy breach detected by independent review: "
                            f"{latest_verdict.summary}. Automated repair halted; awaiting human approval."
                        )
                        self._save_task_state(
                            task_id, "WAITING_APPROVAL", repair_attempts, p0_summary,
                            mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha,
                            reviewer_model=reviewer.model_name, test_passed=latest_artifacts.test_passed,
                        )
                        return ClosedLoopResult(
                            task_id=task_id,
                            success=False,
                            state="WAITING_APPROVAL",
                            result_summary=p0_summary,
                            repair_attempts=repair_attempts,
                            final_commit_sha=latest_artifacts.commit_sha,
                            artifacts=latest_artifacts,
                            verdict=latest_verdict,
                            history=history,
                        )

                    severity = latest_verdict.severity
                    rejection_reason = (
                        f"Review rejection ({severity}): {latest_verdict.summary}\n"
                        f"Critical issues: {latest_verdict.critical_issues}\n"
                        f"Suggestions: {latest_verdict.suggestions}"
                    )

                # -------------------------------------------------------------
                # 3. Failure Retry Orchestration (Repair Loop)
                # -------------------------------------------------------------
                if repair_attempts >= self.max_repair_attempts:
                    exhausted_summary = (
                        f"Repair limit exhausted ({repair_attempts}/{self.max_repair_attempts}). "
                        f"Last rejection: {rejection_reason[:200]}"
                    )
                    self._save_task_state(task_id, "FAILED", repair_attempts, exhausted_summary)
                    return ClosedLoopResult(
                        task_id=task_id,
                        success=False,
                        state="FAILED",
                        result_summary=exhausted_summary,
                        repair_attempts=repair_attempts,
                        final_commit_sha=latest_artifacts.commit_sha,
                        artifacts=latest_artifacts,
                        verdict=latest_verdict,
                        history=history,
                    )

                # Proceed with repair
                repair_attempts += 1
                self._save_task_state(task_id, "REPAIRING", repair_attempts=repair_attempts)
                self._log_step(
                    task_id,
                    f"Gemini Repair Attempt #{repair_attempts}",
                    "PRIMARY_DEVELOPER",
                    dev.model_name,
                    "IN_PROGRESS",
                    output_snippet=rejection_reason[:300],
                )

                repair_prompt = (
                    f"Requirement:\n{requirement}\n\n"
                    f"### Rejection Evidence from Previous Attempt:\n{rejection_reason}\n\n"
                    "Repair the code implementation to resolve these issues. "
                    "Ensure tests pass and the implementation satisfies all requirements."
                )
                repair_res = await dev.execute(repair_prompt, context={"worktree_path": worktree})

                if not repair_res.success:
                    error_msg = f"Gemini repair attempt #{repair_attempts} failed: {repair_res.error}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg)
                    return ClosedLoopResult(
                        task_id=task_id,
                        success=False,
                        state="FAILED",
                        result_summary=error_msg,
                        repair_attempts=repair_attempts,
                        artifacts=latest_artifacts,
                        verdict=latest_verdict,
                        history=history,
                    )

                checkpoint_sha = await self.worktree_mgr.commit_checkpoint(
                    worktree, f"fix: gemini repair attempt #{repair_attempts}"
                )
                repair_paths = self._test_paths(worktree, repair_res.metadata.get("files_written", [])) or test_paths
                runner = TestRunner(worktree, custom_test_cmd=custom_test_cmd, test_paths=repair_paths or None)
                test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

                is_clean = await self.worktree_mgr.is_clean(worktree)
                if not is_clean:
                    error_msg = f"Working directory state modified during test execution in repair attempt #{repair_attempts}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha, test_passed=test_res.passed)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
                if not checkpoint_sha or checkpoint_sha == base_ref or not diff.strip():
                    error_msg = f"Gemini repair attempt #{repair_attempts} produced no committed changes (empty diff or unchanged SHA)"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                latest_artifacts = DevelopmentArtifacts(
                    task_id=task_id,
                    commit_sha=checkpoint_sha,
                    base_ref=base_ref,
                    git_diff=diff,
                    test_passed=test_res.passed,
                    test_summary=test_res.raw_output,
                    generated_files=repair_res.metadata.get("files_written", []),
                    repair_attempts=repair_attempts,
                )

        except BudgetLimitError as err:
            self._save_task_state(task_id, "BUDGET_STOPPED", repair_attempts, str(err), mock_mode=mock_mode)
            return ClosedLoopResult(
                task_id=task_id,
                success=False,
                state="BUDGET_STOPPED",
                result_summary=str(err),
                repair_attempts=repair_attempts,
            )
        except asyncio.CancelledError:
            self._save_task_state(task_id, "INTERRUPTED", repair_attempts, "Closed loop cancelled", mock_mode=mock_mode)
            raise
        except Exception as exc:
            logger.exception("Unexpected error during Gemini closed loop: %s", exc)
            err_msg = f"Fatal execution exception: {exc}"
            self._save_task_state(task_id, "FAILED", repair_attempts, err_msg, mock_mode=mock_mode)
            return ClosedLoopResult(
                task_id=task_id,
                success=False,
                state="FAILED",
                result_summary=err_msg,
                repair_attempts=repair_attempts,
            )

    async def resume_after_approval(
        self,
        task_id: str,
        approved: bool,
        notes: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
        dev_agent: Optional[GeminiDevAgent] = None,
        reviewer_agent: Optional[FlashReviewer] = None,
    ) -> ClosedLoopResult:
        """Resume execution of a Gemini closed-loop task held in WAITING_APPROVAL.

        Maintains the original Gemini developer and independent reviewer context,
        retaining bounded repairs and gate verifications.
        """
        validate_custom_test_cmd(custom_test_cmd)
        with self._provide_session(mock_mode) as s:
            task = s.exec(select(Task).where(Task.task_id == task_id)).first()
            if not task:
                raise ValueError(f"Task '{task_id}' not found.")
            if task.state != "WAITING_APPROVAL":
                raise ValueError(f"Task '{task_id}' is in state '{task.state}', expected 'WAITING_APPROVAL'.")
            requirement = task.requirement
            current_repair_attempts = task.repair_attempts
            stored_worktree = task.worktree_path
            reviewer_model_str = task.reviewer_model

        if not approved:
            decision_note = notes or "Rejected by human operator"
            err_msg = f"Rejected by human operator: {decision_note}"
            self._save_task_state(task_id, "FAILED", current_repair_attempts, err_msg, mock_mode=mock_mode)
            self._log_step(
                task_id,
                "Human Approval Decision",
                "SYSTEM",
                "HumanOperator",
                "FAILED",
                error_snippet=err_msg,
                mock_mode=mock_mode,
            )
            return ClosedLoopResult(
                task_id=task_id,
                success=False,
                state="FAILED",
                result_summary=err_msg,
                repair_attempts=current_repair_attempts,
            )

        decision_note = notes or "Authorized by human operator"
        self._save_task_state(task_id, "REPAIRING", current_repair_attempts, mock_mode=mock_mode)
        self._log_step(
            task_id,
            "Human Approval Decision",
            "SYSTEM",
            "HumanOperator",
            "SUCCESS",
            output_snippet=f"Approved: {decision_note}",
            mock_mode=mock_mode,
        )

        if stored_worktree and Path(stored_worktree).exists():
            worktree = Path(stored_worktree).resolve()
        else:
            worktree = (await self.worktree_mgr.create_worktree(task_id)).resolve()
            self._save_task_state(task_id, "REPAIRING", current_repair_attempts, mock_mode=mock_mode, worktree_path=str(worktree))

        base_ref = await self.worktree_mgr.get_base_ref(worktree)
        dev = dev_agent or GeminiDevAgent(mock_mode=mock_mode)
        rev_choice = "deepseek" if reviewer_model_str and "deepseek" in reviewer_model_str.lower() else self.reviewer_choice
        reviewer = reviewer_agent or FlashReviewer(
            model_key=rev_choice,
            budget_cfg=self.policy.budget,
            mock_mode=mock_mode,
        )

        repair_attempts = current_repair_attempts + 1
        history: list[dict[str, Any]] = []
        latest_artifacts: Optional[DevelopmentArtifacts] = None
        latest_verdict: Optional[ReviewVerdict] = None

        try:
            while True:
                self._save_task_state(task_id, "REPAIRING", repair_attempts=repair_attempts, mock_mode=mock_mode)
                self._log_step(
                    task_id,
                    f"Gemini Approved Repair Attempt #{repair_attempts}",
                    "PRIMARY_DEVELOPER",
                    dev.model_name,
                    "IN_PROGRESS",
                    output_snippet=decision_note[:300],
                    mock_mode=mock_mode,
                )

                repair_prompt = (
                    f"Requirement:\n{requirement}\n\n"
                    f"### Human Approval Instructions:\n{decision_note}\n\n"
                    "Repair the code implementation to resolve the vulnerability or issue. "
                    "Ensure tests pass and satisfy all requirements."
                )
                repair_res = await dev.execute(repair_prompt, context={"worktree_path": worktree})
                if not repair_res.success:
                    error_msg = f"Gemini approved repair #{repair_attempts} failed: {repair_res.error}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                checkpoint_sha = await self.worktree_mgr.commit_checkpoint(
                    worktree, f"fix: gemini approved repair attempt #{repair_attempts}"
                )
                test_paths = self._test_paths(worktree, repair_res.metadata.get("files_written", []))
                runner = TestRunner(worktree, custom_test_cmd=custom_test_cmd, test_paths=test_paths or None)
                test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

                is_clean = await self.worktree_mgr.is_clean(worktree)
                if not is_clean:
                    error_msg = f"Working directory modified during test execution in repair #{repair_attempts}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha, test_passed=test_res.passed)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref)
                if not checkpoint_sha or checkpoint_sha == base_ref or not diff.strip():
                    error_msg = f"Gemini approved repair #{repair_attempts} produced no committed changes (empty diff or unchanged SHA)"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                latest_artifacts = DevelopmentArtifacts(
                    task_id=task_id,
                    commit_sha=checkpoint_sha,
                    base_ref=base_ref,
                    git_diff=diff,
                    test_passed=test_res.passed,
                    test_summary=test_res.raw_output,
                    generated_files=repair_res.metadata.get("files_written", []),
                    repair_attempts=repair_attempts,
                )

                if not latest_artifacts.test_passed:
                    rejection_reason = f"Local tests failed after repair #{repair_attempts}:\n{latest_artifacts.test_summary}"
                else:
                    self._save_task_state(task_id, "REVIEWING", repair_attempts=repair_attempts, mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha, test_passed=latest_artifacts.test_passed)
                    review_prompt = "Review requirements, code implementation, and actual test evidence after authorized repair."
                    review_context = {
                        "requirement": requirement,
                        "expected_commit_sha": latest_artifacts.commit_sha,
                        "git_diff": redact_and_truncate_evidence(latest_artifacts.git_diff),
                        "test_summary": redact_and_truncate_evidence(latest_artifacts.test_summary),
                        "worktree_path": str(worktree),
                    }

                    guard = self._get_budget_guard(mock_mode=mock_mode)
                    reservation_id = guard.reserve_call(task_id, 0.05, simulated=mock_mode)
                    try:
                        review_res = await reviewer.execute(review_prompt, context=review_context)
                    except BaseException:
                        guard.settle_call(reservation_id, 0, 0, cost_cny=None)
                        raise
                    actual_cost = review_res.cost_cny if (review_res.success and review_res.cost_cny is not None) else 0.0
                    guard.settle_call(
                        reservation_id,
                        review_res.prompt_tokens or 0,
                        review_res.completion_tokens or 0,
                        cost_cny=actual_cost,
                    )

                    if not review_res.success:
                        error_msg = f"Independent review failed: {review_res.error}"
                        self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha)
                        return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                    raw_verdict = review_res.metadata.get("review") or {}
                    latest_verdict = ReviewVerdict(
                        approved=bool(raw_verdict.get("approved")),
                        severity=raw_verdict.get("severity", "P2"),
                        summary=raw_verdict.get("summary", ""),
                        critical_issues=raw_verdict.get("critical_issues", []),
                        suggestions=raw_verdict.get("suggestions", []),
                        reviewer_model=reviewer.model_name,
                    )

                    self._log_step(
                        task_id,
                        f"{reviewer.model_key.upper()} Review Callback (Post-Approval)",
                        "REVIEWER",
                        reviewer.model_name,
                        "SUCCESS" if latest_verdict.approved else "REJECTED",
                        output_snippet=json.dumps(asdict(latest_verdict), ensure_ascii=False),
                        cost_cny=review_res.cost_cny,
                        mock_mode=mock_mode,
                    )

                    history.append({
                        "attempt": repair_attempts,
                        "commit_sha": latest_artifacts.commit_sha,
                        "test_passed": latest_artifacts.test_passed,
                        "review": asdict(latest_verdict),
                    })

                    if latest_verdict.approved and latest_verdict.severity not in ("P0", "P1"):
                        success_summary = (
                            f"Verified closed-loop delivery after human approval: Gemini repair and "
                            f"{reviewer.model_key.upper()} independent review passed after {repair_attempts} repairs."
                        )
                        self._save_task_state(
                            task_id, "SUCCESS", repair_attempts, success_summary,
                            mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha,
                            reviewer_model=reviewer.model_name, test_passed=latest_artifacts.test_passed,
                        )
                        return ClosedLoopResult(
                            task_id=task_id,
                            success=True,
                            state="SUCCESS",
                            result_summary=success_summary,
                            repair_attempts=repair_attempts,
                            final_commit_sha=latest_artifacts.commit_sha,
                            artifacts=latest_artifacts,
                            verdict=latest_verdict,
                            history=history,
                        )

                    if latest_verdict.severity == "P0":
                        p0_summary = (
                            f"P0 critical vulnerability re-detected after repair attempt #{repair_attempts}: "
                            f"{latest_verdict.summary}. Awaiting further human approval."
                        )
                        self._save_task_state(
                            task_id, "WAITING_APPROVAL", repair_attempts, p0_summary,
                            mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha,
                            reviewer_model=reviewer.model_name, test_passed=latest_artifacts.test_passed,
                        )
                        return ClosedLoopResult(
                            task_id=task_id,
                            success=False,
                            state="WAITING_APPROVAL",
                            result_summary=p0_summary,
                            repair_attempts=repair_attempts,
                            final_commit_sha=latest_artifacts.commit_sha,
                            artifacts=latest_artifacts,
                            verdict=latest_verdict,
                            history=history,
                        )

                    rejection_reason = (
                        f"Review rejection ({latest_verdict.severity}): {latest_verdict.summary}\n"
                        f"Critical issues: {latest_verdict.critical_issues}\n"
                        f"Suggestions: {latest_verdict.suggestions}"
                    )

                if repair_attempts >= self.max_repair_attempts:
                    exhausted_summary = (
                        f"Repair limit exhausted ({repair_attempts}/{self.max_repair_attempts}). "
                        f"Last rejection: {rejection_reason[:200]}"
                    )
                    self._save_task_state(task_id, "FAILED", repair_attempts, exhausted_summary, mock_mode=mock_mode)
                    return ClosedLoopResult(
                        task_id=task_id,
                        success=False,
                        state="FAILED",
                        result_summary=exhausted_summary,
                        repair_attempts=repair_attempts,
                        final_commit_sha=latest_artifacts.commit_sha if latest_artifacts else None,
                        artifacts=latest_artifacts,
                        verdict=latest_verdict,
                        history=history,
                    )

                repair_attempts += 1
                decision_note = rejection_reason
        except BudgetLimitError as err:
            self._save_task_state(task_id, "BUDGET_STOPPED", repair_attempts, str(err), mock_mode=mock_mode)
            return ClosedLoopResult(task_id=task_id, success=False, state="BUDGET_STOPPED", result_summary=str(err), repair_attempts=repair_attempts)
        except Exception as exc:
            logger.exception("Unexpected error during resume_after_approval: %s", exc)
            err_msg = f"Fatal execution exception during resume: {exc}"
            self._save_task_state(task_id, "FAILED", repair_attempts, err_msg, mock_mode=mock_mode)
            return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=err_msg, repair_attempts=repair_attempts)
