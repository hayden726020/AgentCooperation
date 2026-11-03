"""Run submission and query helpers for the live run console."""

from __future__ import annotations

import fnmatch
import glob as globlib
import threading
from pathlib import Path
from typing import Any, Optional

from execution.events import EventStore, event_store
from orchestrator.gemini_closed_loop import GeminiClosedLoopOrchestrator
from web.runner import job_manager

CLOSED_LOOP_MODEL = "gemini-3.8-flash-high"
REVIEWER_CHOICES = ("glm", "deepseek")
MAX_REPAIR_LIMIT = 10


class RunValidationError(ValueError):
    """Raised when a run request cannot be safely dispatched."""


class RunConflictError(RuntimeError):
    """Raised when a run is already being resumed."""


_APPROVAL_LOCK = threading.Lock()
_APPROVALS_INFLIGHT: set[str] = set()


def _allowed_patterns(policy: Any) -> list[str]:
    return [str(item) for item in (policy.security.allowed_repos or ["."]) if str(item).strip()]


def _has_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def _expand_allowed_roots(project_root: Path, policy: Any) -> list[Path]:
    roots: list[Path] = []
    for pattern in _allowed_patterns(policy):
        candidate = Path(pattern)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if _has_glob(pattern):
            for match in sorted(globlib.glob(str(candidate), recursive=True)):
                if Path(match).is_dir():
                    roots.append(Path(match).resolve())
        else:
            roots.append(candidate.resolve())
    return roots


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def available_repos(project_root: Path, policy: Any) -> list[dict[str, Any]]:
    """Return switchable Git repositories authorized by policy.security.allowed_repos."""
    seen: dict[Path, dict[str, Any]] = {}

    def add(path: Path, source: str) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not resolved.is_dir() or not _is_git_repo(resolved):
            return
        seen[resolved] = {
            "path": str(resolved),
            "name": resolved.name or str(resolved),
            "source": source,
        }

    for root in _expand_allowed_roots(project_root, policy):
        add(root, "allowed")
        # Direct Git children keep multi-repo switching useful without
        # authorizing anything outside the configured root.
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                add(child, "child")

    project = project_root.resolve()
    if _is_git_repo(project):
        add(project, "project")
    repos = list(seen.values())
    repos.sort(key=lambda item: (item["path"] != str(project), item["name"].lower()))
    return repos


def resolve_repo(raw: Optional[str], project_root: Path, policy: Any) -> Path:
    """Resolve and authorize a requested repository path."""
    text = raw.strip().strip("'\"") if isinstance(raw, str) else ""
    target = Path(text) if text else project_root
    if not target.is_absolute():
        target = project_root / target
    target = target.resolve()

    if not target.is_dir() or not _is_git_repo(target):
        raise RunValidationError(f"Not a Git repository: {target}")

    roots = _expand_allowed_roots(project_root, policy)
    authorized = any(target == root or target.is_relative_to(root) for root in roots)
    if not authorized:
        try:
            relative = target.relative_to(project_root.resolve()).as_posix()
        except ValueError:
            relative = None
        if relative is not None:
            authorized = any(
                _has_glob(pattern) and fnmatch.fnmatch(relative, pattern)
                for pattern in _allowed_patterns(policy)
            )
    if not authorized:
        raise PermissionError(
            f"Unauthorized repository path: '{target}'. "
            f"Configure security.allowed_repos to authorize it."
        )
    return target


def _coerce_max_repair(raw: Any, policy: Any) -> int:
    if raw is None:
        return int(getattr(policy.repair, "max_normal_attempts", 3))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RunValidationError("max_repair must be an integer")
    if raw < 0 or raw > MAX_REPAIR_LIMIT:
        raise RunValidationError(f"max_repair must be between 0 and {MAX_REPAIR_LIMIT}")
    return raw


def _coerce_test_paths(raw: Any) -> Optional[list[str]]:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise RunValidationError("test_paths must be a list of relative paths")
    paths = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise RunValidationError("test_paths must contain non-empty strings")
        paths.append(item.strip())
    return paths or None


def submit_run(
    *,
    requirement: str,
    project_root: Path,
    policy: Any,
    repo: Optional[str] = None,
    reviewer: Optional[str] = None,
    max_repair: Any = None,
    test_paths: Any = None,
    title: Optional[str] = None,
    allow_untrusted_tests: bool = False,
    mock: bool = False,
    store: Optional[EventStore] = None,
) -> dict[str, Any]:
    """Validate a run request, dispatch it, and return its identifiers immediately."""
    if requirement is not None and not isinstance(requirement, str):
        raise RunValidationError("requirement must be a string")
    text = (requirement or "").strip()
    if not text:
        raise RunValidationError("requirement is required")

    if reviewer is not None and not isinstance(reviewer, str):
        raise RunValidationError("reviewer must be a string")
    reviewer_key = (reviewer or "glm").strip().lower()
    if reviewer_key not in REVIEWER_CHOICES:
        raise RunValidationError(f"reviewer must be one of {list(REVIEWER_CHOICES)}")

    if repo is not None and not isinstance(repo, str):
        raise RunValidationError("repo must be a string")
    if title is not None and not isinstance(title, str):
        raise RunValidationError("title must be a string")
    title = title.strip() if isinstance(title, str) else None

    repair_limit = _coerce_max_repair(max_repair, policy)
    safe_paths = _coerce_test_paths(test_paths)
    target_repo = resolve_repo(repo, project_root, policy)

    task_id = GeminiClosedLoopOrchestrator._new_task_id()
    job_id = job_manager.create_job(
        "closed_loop_run",
        metadata={
            "task_id": task_id,
            "title": title or text[:40],
            "requirement": text,
            "repo": str(target_repo),
            "reviewer": reviewer_key,
            "max_repair": repair_limit,
            "mock": mock,
        },
    )

    async def _run() -> dict[str, Any]:
        orchestrator = GeminiClosedLoopOrchestrator(
            repo_root=target_repo,
            policy=policy,
            reviewer_choice=reviewer_key,
            max_repair_attempts=repair_limit,
            event_store=store or event_store,
        )
        result = await orchestrator.run(
            requirement=text,
            title=title or text[:40],
            mock_mode=mock,
            test_paths=safe_paths,
            allow_untrusted_tests=allow_untrusted_tests,
            task_id=task_id,
        )
        return {
            "task_id": result.task_id,
            "state": result.state,
            "success": result.success,
            "repair_attempts": result.repair_attempts,
            "final_commit_sha": result.final_commit_sha,
            "summary": result.result_summary,
        }

    job_manager.run_async(job_id, _run)
    return {
        "task_id": task_id,
        "job_id": job_id,
        "repo": str(target_repo),
        "reviewer": reviewer_key,
        "max_repair": repair_limit,
        "mock": mock,
    }


def start_approval(
    *,
    task_id: str,
    approved: bool,
    notes: Optional[str],
    repo_root: str,
    reviewer_model: Optional[str],
    mock_mode: bool,
    project_root: Path,
    policy: Any,
    store: Optional[EventStore] = None,
    allow_untrusted_tests: bool = False,
) -> dict[str, Any]:
    """Dispatch a human approval decision for a paused closed-loop run."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise RunValidationError("task_id is required")
    if not isinstance(approved, bool):
        raise RunValidationError("approved must be a boolean")
    if not isinstance(allow_untrusted_tests, bool):
        raise RunValidationError("allow_untrusted_tests must be a boolean")
    if notes is not None and not isinstance(notes, str):
        raise RunValidationError("notes must be a string")
    decision_notes = (notes or "").strip()[:2000]
    untrusted_allowed = allow_untrusted_tests and approved

    # Rejection only records a terminal state; do not require a still-valid repo.
    target_repo = (
        resolve_repo(repo_root, project_root, policy)
        if approved
        else Path(project_root).resolve()
    )
    reviewer_choice = "deepseek" if "deepseek" in (reviewer_model or "").lower() else "glm"

    with _APPROVAL_LOCK:
        if task_id in _APPROVALS_INFLIGHT:
            raise RunConflictError(f"Run '{task_id}' is already being resumed")
        _APPROVALS_INFLIGHT.add(task_id)

    try:
        job_id = job_manager.create_job(
            "closed_loop_approval",
            metadata={
                "task_id": task_id,
                "approved": approved,
                "notes": decision_notes,
                "repo": str(target_repo),
                "reviewer": reviewer_choice,
                "mock": mock_mode,
                "allow_untrusted_tests": untrusted_allowed,
            },
        )

        async def _run() -> dict[str, Any]:
            try:
                orchestrator = GeminiClosedLoopOrchestrator(
                    repo_root=target_repo,
                    policy=policy,
                    reviewer_choice=reviewer_choice,
                    event_store=store or event_store,
                )
                result = await orchestrator.resume_after_approval(
                    task_id=task_id,
                    approved=approved,
                    notes=decision_notes or None,
                    mock_mode=mock_mode,
                    allow_untrusted_tests=untrusted_allowed,
                )
                return {
                    "task_id": result.task_id,
                    "state": result.state,
                    "success": result.success,
                    "repair_attempts": result.repair_attempts,
                    "final_commit_sha": result.final_commit_sha,
                    "summary": result.result_summary,
                }
            finally:
                with _APPROVAL_LOCK:
                    _APPROVALS_INFLIGHT.discard(task_id)

        job_manager.run_async(job_id, _run)
    except BaseException:
        with _APPROVAL_LOCK:
            _APPROVALS_INFLIGHT.discard(task_id)
        raise

    return {
        "task_id": task_id,
        "job_id": job_id,
        "approved": approved,
        "repo": str(target_repo),
        "reviewer": reviewer_choice,
        "mock": mock_mode,
    }
