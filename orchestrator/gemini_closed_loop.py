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
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)(postgres|postgresql|mysql|mongodb|redis):\/\/[^\s:]+:[^\s@]+@[^\s\/]+"), r"\1://[REDACTED_DB_CREDENTIALS]"),
    (re.compile(r"Bearer\s+ya29\.[A-Za-z0-9_\-]+", re.IGNORECASE), "Bearer [REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"ya29\.[A-Za-z0-9_\-]+"), "[REDACTED_OAUTH_TOKEN]"),
]

_SENSITIVE_FILES_PATTERN = re.compile(
    r"(^|[\/\\])(\.env(\..+)?|\.git|\.aws|\.codex|id_rsa|id_ed25519|.*\.pem|.*\.key)$",
    re.IGNORECASE,
)


def contains_sensitive_files(paths: list[str]) -> tuple[bool, str]:
    """Check if any path matches sensitive patterns (.env, .git, .aws, keys, certs)."""
    for p in paths:
        p_clean = p.replace("\\", "/").strip("/")
        parts = p_clean.split("/")
        for part in parts:
            if _SENSITIVE_FILES_PATTERN.match(part):
                return True, p
        if _SENSITIVE_FILES_PATTERN.match(p_clean):
            return True, p
    return False, ""


def redact_and_truncate_evidence(content: str, max_chars: int = 40000) -> str:
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
    forbidden_patterns = [
        r"[;&|`$<>\n\r]",
        r"\b(rm|del|rmdir|shutil|curl|wget|bash|sh|cmd|powershell|pwsh)\b",
        r"(?:^|\s)-p\b",
        r"(?:^|\s)--rootdir\b",
        r"(?:^|\s)--confcutdir\b",
        r"\.\.",
    ]
    for pat in forbidden_patterns:
        if re.search(pat, cmd, re.IGNORECASE):
            raise ValueError(f"Custom test command contains disallowed patterns: {cmd}")
    allowed_prefixes = ("python -m pytest", "python.exe -m pytest", "pytest", "pytest.exe")
    cleaned = cmd.strip()
    matched = False
    for prefix in allowed_prefixes:
        if cleaned == prefix or cleaned.startswith(prefix + " "):
            matched = True
            break
    if not matched:
        raise ValueError(f"Custom test command must start with approved test runner (python -m pytest / pytest): {cmd}")


def validate_safe_test_paths(paths: Optional[list[str]], worktree: Optional[Path] = None) -> list[str]:
    """Validate relative test paths against shell injections, path traversals, flags and symlinks."""
    if not paths:
        return []
    safe_paths: list[str] = []
    forbidden_chars = [";", "&", "|", "`", "$", "<", ">", "\n", "\r", "\x00"]
    for p in paths:
        p_str = str(p).strip()
        if not p_str:
            continue
        for ch in forbidden_chars:
            if ch in p_str:
                raise ValueError(f"Disallowed character '{ch}' in test path: {p_str}")
        if p_str.startswith("-"):
            raise ValueError(f"Flag arguments not permitted in test paths: {p_str}")
        p_obj = Path(p_str)
        if p_obj.is_absolute() or p_str.startswith("/") or p_str.startswith("\\"):
            raise ValueError(f"Absolute test paths disallowed: {p_str}")
        if ".." in p_obj.parts:
            raise ValueError(f"Path traversal ('..') disallowed: {p_str}")
        if worktree is not None:
            resolved_worktree = worktree.resolve()
            target = (worktree / p_str).resolve()
            try:
                target.relative_to(resolved_worktree)
            except ValueError:
                raise ValueError(f"Test path resolves outside worktree: {p_str}")
            if not target.exists():
                raise ValueError(f"Test path does not exist: {p_str}")
            if target.is_symlink():
                raise ValueError(f"Symlink test paths disallowed: {p_str}")
        safe_paths.append(p_str)
    return safe_paths


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
    tree_hash: Optional[str] = None
    evidence_id: Optional[str] = None


@dataclass
class ReviewVerdict:
    """Structured callback verdict returned by the independent reviewer."""

    approved: bool
    severity: Literal["P0", "P1", "P2", "P3"]
    summary: str
    critical_issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reviewer_model: str = "glm-5.3-flashx"
    server_model: Optional[str] = None


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

    def _new_dev_agent(self, mock_mode: bool) -> GeminiDevAgent:
        """Create a Gemini developer with the bounded policy timeout."""
        return GeminiDevAgent(
            mock_mode=mock_mode,
            timeout=float(self.policy.execution.model_timeout_seconds),
        )

    @staticmethod
    def _is_production_session(session: Optional[Session]) -> bool:
        if session is None:
            return False
        try:
            bind = session.get_bind()
            if bind and bind.url:
                db_name = str(bind.url.database or "")
                prod_name = Path(settings.sqlite_db_path).name
                if db_name.endswith(prod_name) and not db_name.endswith("mock_" + prod_name):
                    return True
        except Exception:
            pass
        return False

    def _provide_session(self, mock_mode: bool = False):
        if self.session is not None:
            if not (mock_mode and self._is_production_session(self.session)):
                return provide_session(self.session)
        if mock_mode:
            mock_db_path = PROJECT_ROOT / settings.mock_sqlite_db_path
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
            if not (mock_mode and self._is_production_session(self.session)):
                return BudgetGuard(self.policy.budget, self.session)
        if mock_mode:
            mock_db_path = PROJECT_ROOT / settings.mock_sqlite_db_path
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
            for path in (*tests_dir.rglob("test_*.py"), *tests_dir.rglob("*_test.py")):
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

    @staticmethod
    def _detect_modified_or_new_tests(files: list[str]) -> list[str]:
        tests = []
        for f in files:
            p = Path(f)
            name = p.name
            normalized = f.replace("\\", "/")
            if name.startswith("test_") or name.endswith("_test.py") or "tests/" in normalized:
                tests.append(f)
        return sorted(set(tests))

    async def run(
        self,
        requirement: str,
        title: Optional[str] = None,
        mock_mode: bool = False,
        custom_test_cmd: Optional[str] = None,
        test_paths: Optional[list[str]] = None,
        allow_untrusted_tests: bool = False,
        dev_agent: Optional[GeminiDevAgent] = None,
        reviewer_agent: Optional[FlashReviewer] = None,
        task_id: Optional[str] = None,
    ) -> ClosedLoopResult:

        if custom_test_cmd:
            validate_custom_test_cmd(custom_test_cmd)
        if test_paths:
            validate_safe_test_paths(test_paths)

        if not requirement or not requirement.strip():
            raise ValueError("Task requirement must not be empty")

        if not mock_mode:
            can_start, budget_msg = self.budget_guard.check_can_start_new_task()
            if not can_start:
                raise BudgetLimitError(budget_msg)

        task_id = task_id or f"task_gemini_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        title = title or requirement[:40]

        # Initialize Task record with mock_mode isolation and repo_root
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
                    repo_root=str(self.repo_root),
                )
            )
            s.commit()

        # Instantiate agents if not provided
        dev = dev_agent or self._new_dev_agent(mock_mode)
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

            # Security Check: Reject any sensitive files
            has_sens, sens_path = contains_sensitive_files(files_written)
            if has_sens:
                error_msg = f"Security violation: generated changes touch sensitive path: {sens_path}"
                self._save_task_state(task_id, "FAILED", 0, error_msg, mock_mode=mock_mode)
                self._log_step(task_id, "Gate: Sensitive Path Verification", "SECURITY", "Gate", "FAILED", error_snippet=error_msg, mock_mode=mock_mode)
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

            # Check untrusted test files
            untrusted_tests = self._detect_modified_or_new_tests(files_written)
            if untrusted_tests and not allow_untrusted_tests and not mock_mode:
                wait_msg = (
                    f"Untrusted test code generated or modified without OS sandbox isolation: {untrusted_tests}. "
                    "Paused for human approval before execution."
                )
                self._save_task_state(task_id, "WAITING_APPROVAL", 0, wait_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                self._log_step(task_id, "Sandbox Gate: Untrusted Test Code", "SECURITY", "Gate", "WAITING_APPROVAL", output_snippet=wait_msg, mock_mode=mock_mode)
                return ClosedLoopResult(
                    task_id=task_id,
                    success=False,
                    state="WAITING_APPROVAL",
                    result_summary=wait_msg,
                    repair_attempts=0,
                    final_commit_sha=checkpoint_sha,
                )

            # Capture HEAD and tree hash before running tests
            head_before = await self.worktree_mgr.get_head_sha(worktree)
            tree_before = await self.worktree_mgr.get_tree_hash(worktree, checkpoint_sha)

            # Run test verification
            chosen_tests = validate_safe_test_paths(test_paths, worktree) if test_paths else self._test_paths(worktree, files_written)
            runner = TestRunner(worktree, test_paths=chosen_tests or None)
            test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

            # Gate 3: Verify clean working directory, unchanged HEAD and tree hash after test
            is_clean = await self.worktree_mgr.is_clean(worktree)
            head_after = await self.worktree_mgr.get_head_sha(worktree)
            tree_after = await self.worktree_mgr.get_tree_hash(worktree, "HEAD")
            if not is_clean or head_after != head_before or tree_after != tree_before:
                error_msg = "Working directory state modified during test execution (uncommitted changes or rogue commit detected)"
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

            diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref, target_ref=checkpoint_sha)
            latest_artifacts = DevelopmentArtifacts(
                task_id=task_id,
                commit_sha=checkpoint_sha,
                base_ref=base_ref,
                git_diff=diff,
                test_passed=test_res.passed,
                test_summary=test_res.raw_output,
                generated_files=files_written,
                repair_attempts=repair_attempts,
                tree_hash=tree_before,
                evidence_id=str(uuid.uuid4())[:8],
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
                    server_model = review_res.metadata.get("server_model") or reviewer.model_name
                    latest_verdict = ReviewVerdict(
                        approved=bool(raw_verdict.get("approved")),
                        severity=raw_verdict.get("severity", "P2"),
                        summary=raw_verdict.get("summary", ""),
                        critical_issues=raw_verdict.get("critical_issues", []),
                        suggestions=raw_verdict.get("suggestions", []),
                        reviewer_model=reviewer.model_name,
                        server_model=server_model,
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
                    self._save_task_state(task_id, "FAILED", repair_attempts, exhausted_summary, mock_mode=mock_mode)
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
                self._save_task_state(task_id, "REPAIRING", repair_attempts=repair_attempts, mock_mode=mock_mode)
                self._log_step(
                    task_id,
                    f"Gemini Repair Attempt #{repair_attempts}",
                    "PRIMARY_DEVELOPER",
                    dev.model_name,
                    "IN_PROGRESS",
                    output_snippet=rejection_reason[:300],
                    mock_mode=mock_mode,
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
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode)
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

                repair_files = repair_res.metadata.get("files_written", [])
                has_sens, sens_path = contains_sensitive_files(repair_files)
                if has_sens:
                    error_msg = f"Security violation in repair #{repair_attempts}: touched sensitive path: {sens_path}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                checkpoint_sha = await self.worktree_mgr.commit_checkpoint(
                    worktree, f"fix: gemini repair attempt #{repair_attempts}"
                )
                head_before = await self.worktree_mgr.get_head_sha(worktree)
                tree_before = await self.worktree_mgr.get_tree_hash(worktree, checkpoint_sha)

                repair_paths = self._test_paths(worktree, repair_files) or (validate_safe_test_paths(test_paths, worktree) if test_paths else None)
                runner = TestRunner(worktree, test_paths=repair_paths or None)
                test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

                is_clean = await self.worktree_mgr.is_clean(worktree)
                head_after = await self.worktree_mgr.get_head_sha(worktree)
                tree_after = await self.worktree_mgr.get_tree_hash(worktree, "HEAD")
                if not is_clean or head_after != head_before or tree_after != tree_before:
                    error_msg = f"Working directory state modified during test execution in repair attempt #{repair_attempts}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha, test_passed=test_res.passed)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref, target_ref=checkpoint_sha)
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
                    generated_files=repair_files,
                    repair_attempts=repair_attempts,
                    tree_hash=tree_before,
                    evidence_id=str(uuid.uuid4())[:8],
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
        test_paths: Optional[list[str]] = None,
        allow_untrusted_tests: bool = True,
        dev_agent: Optional[GeminiDevAgent] = None,
        reviewer_agent: Optional[FlashReviewer] = None,
    ) -> ClosedLoopResult:
        """Resume execution of a Gemini closed-loop task held in WAITING_APPROVAL.

        Maintains the original Gemini developer and independent reviewer context,
        retaining bounded repairs and gate verifications.
        """
        if custom_test_cmd:
            validate_custom_test_cmd(custom_test_cmd)
        if test_paths:
            validate_safe_test_paths(test_paths)

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
            stored_repo_root = task.repo_root
            pre_p0_checkpoint = task.commit_sha

        # Validate task repository root
        if stored_repo_root:
            stored_path = Path(stored_repo_root).resolve()
            if stored_path != self.repo_root:
                if not (stored_path / ".git").exists():
                    raise ValueError(f"Task stored repo root is invalid: {stored_repo_root}")

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

        if not (worktree / ".git").exists():
            raise ValueError(f"Worktree has no git repository metadata: {worktree}")

        base_ref = await self.worktree_mgr.get_base_ref(worktree)
        dev = dev_agent or self._new_dev_agent(mock_mode)
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

                repair_files = repair_res.metadata.get("files_written", [])
                native_tools = repair_res.metadata.get("native_tool_steps", [])
                has_write = any(
                    item.get("name") in ("write_to_file", "replace_file_content") and item.get("succeeded")
                    for item in native_tools
                ) or bool(repair_files)
                if not mock_mode and not has_write:
                    error_msg = f"Gemini approved repair #{repair_attempts} produced no native file modifications"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                has_sens, sens_path = contains_sensitive_files(repair_files)
                if has_sens:
                    error_msg = f"Security violation in repair #{repair_attempts}: touched sensitive path: {sens_path}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                checkpoint_sha = await self.worktree_mgr.commit_checkpoint(
                    worktree, f"fix: gemini approved repair attempt #{repair_attempts}"
                )
                if not checkpoint_sha or checkpoint_sha == pre_p0_checkpoint:
                    error_msg = f"Gemini approved repair #{repair_attempts} produced no new commit or unchanged SHA after approval"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                head_before = await self.worktree_mgr.get_head_sha(worktree)
                tree_before = await self.worktree_mgr.get_tree_hash(worktree, checkpoint_sha)

                repair_paths = self._test_paths(worktree, repair_files) or (validate_safe_test_paths(test_paths, worktree) if test_paths else None)
                runner = TestRunner(worktree, test_paths=repair_paths or None)
                test_res = await runner.run(timeout=self.policy.execution.timeout_seconds)

                is_clean = await self.worktree_mgr.is_clean(worktree)
                head_after = await self.worktree_mgr.get_head_sha(worktree)
                tree_after = await self.worktree_mgr.get_tree_hash(worktree, "HEAD")
                if not is_clean or head_after != head_before or tree_after != tree_before:
                    error_msg = f"Working directory modified during test execution in repair #{repair_attempts}"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha, test_passed=test_res.passed)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                diff = await self.worktree_mgr.get_diff(worktree, base_ref=base_ref, target_ref=checkpoint_sha)
                if not diff.strip():
                    error_msg = f"Gemini approved repair #{repair_attempts} produced empty diff relative to base"
                    self._save_task_state(task_id, "FAILED", repair_attempts, error_msg, mock_mode=mock_mode, commit_sha=checkpoint_sha)
                    return ClosedLoopResult(task_id=task_id, success=False, state="FAILED", result_summary=error_msg, repair_attempts=repair_attempts)

                latest_artifacts = DevelopmentArtifacts(
                    task_id=task_id,
                    commit_sha=checkpoint_sha,
                    base_ref=base_ref,
                    git_diff=diff,
                    test_passed=test_res.passed,
                    test_summary=test_res.raw_output,
                    generated_files=repair_files,
                    repair_attempts=repair_attempts,
                    tree_hash=tree_before,
                    evidence_id=str(uuid.uuid4())[:8],
                )

                if not latest_artifacts.test_passed:
                    rejection_reason = f"Local tests failed after repair #{repair_attempts}:\n{latest_artifacts.test_summary}"
                else:
                    self._save_task_state(task_id, "REVIEWING", repair_attempts=repair_attempts, mock_mode=mock_mode, commit_sha=latest_artifacts.commit_sha, test_passed=latest_artifacts.test_passed)
                    review_prompt = "Review requirements, code implementation, and actual test evidence after authorized repair."
                    review_context = {
                        "requirement": requirement,
                        "expected_commit_sha": latest_artifacts.commit_sha,
                        "git_diff": redact_and_truncate_evidence(latest_artifacts.git_diff, max_chars=40000),
                        "test_summary": redact_and_truncate_evidence(latest_artifacts.test_summary, max_chars=80000),
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
                    server_model = review_res.metadata.get("server_model") or reviewer.model_name
                    latest_verdict = ReviewVerdict(
                        approved=bool(raw_verdict.get("approved")),
                        severity=raw_verdict.get("severity", "P2"),
                        summary=raw_verdict.get("summary", ""),
                        critical_issues=raw_verdict.get("critical_issues", []),
                        suggestions=raw_verdict.get("suggestions", []),
                        reviewer_model=reviewer.model_name,
                        server_model=server_model,
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
