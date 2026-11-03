import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from execution.subproc import run_async_command


@dataclass
class WorktreeStatusProbe:
    """Snapshot of a task worktree's state, used for timeout idempotency and state confirmation."""
    worktree_path: Path
    base_ref: str
    head_sha: str
    commits_ahead: int
    is_clean: bool
    staged_files: list[str] = field(default_factory=list)
    unstaged_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    has_meaningful_work: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree_path": str(self.worktree_path),
            "base_ref": self.base_ref,
            "head_sha": self.head_sha,
            "commits_ahead": self.commits_ahead,
            "is_clean": self.is_clean,
            "staged_files": list(self.staged_files),
            "unstaged_files": list(self.unstaged_files),
            "untracked_files": list(self.untracked_files),
            "has_meaningful_work": self.has_meaningful_work,
            "summary": self.summary,
        }


class WorktreeManager:
    """Manage verified task worktrees without modifying the source checkout."""

    def __init__(self, repo_root: Path, worktree_base_dir: str = ".worktrees"):
        self.repo_root = Path(repo_root).resolve()
        self.worktree_base = (self.repo_root / worktree_base_dir).resolve()
        if self.worktree_base == self.repo_root or not self.worktree_base.is_relative_to(self.repo_root):
            raise ValueError("Worktree directory must be inside the repository")

    @staticmethod
    def _branch(task_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id):
            raise ValueError("Invalid task id")
        return f"codex/task_{task_id}"

    def _task_path(self, task_id: str) -> Path:
        self._branch(task_id)
        path = self.worktree_base / task_id
        if path.resolve().parent != self.worktree_base:
            raise ValueError("Worktree path escaped its task directory")
        return path

    async def _git(self, args: list[str], cwd: Path):
        result = await run_async_command(["git", *args], cwd=cwd)
        if not result.is_success:
            raise RuntimeError(f"Git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    async def _validate_worktree(self, worktree_path: Path) -> Path:
        path = Path(worktree_path).resolve()
        if path == self.repo_root or path.parent != self.worktree_base:
            raise ValueError("Operation requires a task worktree inside the configured directory")
        branch = self._branch(path.name)
        marker = path / ".git"
        if not marker.is_file():
            raise ValueError("Task directory is not a linked Git worktree")
        top = await self._git(["rev-parse", "--show-toplevel"], path)
        actual_branch = await self._git(["branch", "--show-current"], path)
        common = await self._git(["rev-parse", "--git-common-dir"], path)
        source_common = await self._git(["rev-parse", "--git-common-dir"], self.repo_root)
        if (Path(top).resolve() != path or actual_branch != branch
                or (path / common).resolve() != (self.repo_root / source_common).resolve()):
            raise ValueError("Task worktree does not belong to this repository and branch")
        return path

    async def ensure_repo_has_initial_commit(self) -> str:
        """Require an existing commit; initializing a user's repository is explicit work."""
        return await self._git(["rev-parse", "--verify", "HEAD"], self.repo_root)

    async def create_worktree(self, task_id: str) -> Path:
        path = self._task_path(task_id)
        branch = self._branch(task_id)
        if path.exists():
            await self._validate_worktree(path)
            await self.get_base_ref(path)
            return path
        base = await self.ensure_repo_has_initial_commit()
        self.worktree_base.mkdir(parents=True, exist_ok=True)
        await self._git(["worktree", "add", "-b", branch, str(path), base], self.repo_root)
        await self._validate_worktree(path)
        await self._git(["config", f"branch.{branch}.orchestratorBase", base], path)
        return path

    async def get_base_ref(self, worktree_path: Path) -> str:
        path = await self._validate_worktree(worktree_path)
        base = await self._git(["config", "--get", f"branch.{self._branch(path.name)}.orchestratorBase"], path)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", base):
            raise ValueError("Task worktree has no valid fixed base commit")
        await self._git(["rev-parse", "--verify", f"{base}^{{commit}}"], path)
        return base

    async def commit_checkpoint(self, worktree_path: Path, message: str) -> str:
        path = await self._validate_worktree(worktree_path)
        await self._git(["add", "-A"], path)
        changed = await run_async_command(["git", "diff", "--cached", "--quiet"], cwd=path)
        if changed.exit_code == 1 and not changed.timed_out:
            await self._git(["commit", "-m", message], path)
        elif not changed.is_success:
            raise RuntimeError(f"Cannot inspect staged checkpoint: {changed.stderr}")
        return await self._git(["rev-parse", "HEAD"], path)

    async def get_head_sha(self, worktree_path: Path) -> str:
        path = await self._validate_worktree(worktree_path)
        return await self._git(["rev-parse", "HEAD"], path)

    async def get_tree_hash(self, worktree_path: Path, ref: str = "HEAD") -> str:
        path = await self._validate_worktree(worktree_path)
        return await self._git(["rev-parse", f"{ref}^{{tree}}"], path)

    async def get_diff(self, worktree_path: Path, base_ref: str | None = None, target_ref: str | None = None) -> str:
        path = await self._validate_worktree(worktree_path)
        base = base_ref or await self.get_base_ref(path)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", base):
            raise ValueError("Diff requires a fixed base commit SHA")
        if target_ref:
            if not re.fullmatch(r"[a-fA-F0-9]{40,64}", target_ref):
                raise ValueError("Target diff requires a fixed commit SHA")
            return await self._git(["diff", "--no-ext-diff", f"{base}..{target_ref}", "--"], path)
        return await self._git(["diff", "--no-ext-diff", base, "--"], path)

    async def rollback(self, worktree_path: Path, target_sha: str) -> bool:
        path = await self._validate_worktree(worktree_path)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", target_sha):
            raise ValueError("Rollback requires a checkpoint commit SHA")
        await self._git(["reset", "--hard", target_sha], path)
        return True

    async def cleanup_worktree(self, task_id: str, force: bool = False):
        path = await self._validate_worktree(self._task_path(task_id))
        cmd = ["worktree", "remove"]
        if force:
            cmd.append("--force")
        await self._git([*cmd, str(path)], self.repo_root)

    async def is_clean(self, worktree_path: Path) -> bool:
        path = await self._validate_worktree(worktree_path)
        status = await self._git(["status", "--porcelain"], path)
        return not bool(status.strip())

    async def get_changed_paths_since(self, worktree_path: Path, base_ref: str) -> list[str]:
        """Return every path changed between the fixed base and HEAD, including deletions."""
        path = await self._validate_worktree(worktree_path)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", base_ref):
            raise ValueError("Changed paths require a fixed base commit SHA")
        res = await run_async_command(
            ["git", "diff", "--name-only", "-z", "--no-renames", base_ref, "HEAD", "--"],
            cwd=path,
        )
        if not res.is_success:
            raise RuntimeError(f"Git diff failed: {res.stderr.strip()}")
        return sorted({entry for entry in res.stdout.split("\0") if entry})

    async def get_changed_paths(self, worktree_path: Path, *, include_ignored: bool = False) -> list[str]:
        """Return staged, unstaged and untracked paths relative to the worktree."""
        path = await self._validate_worktree(worktree_path)
        cmd = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        if include_ignored:
            cmd.append("--ignored=matching")
        res = await run_async_command(cmd, cwd=path)
        if not res.is_success:
            raise RuntimeError(f"Git status failed: {res.stderr.strip()}")
        entries = res.stdout.split("\0")
        paths: set[str] = set()
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            status_code = entry[:2]
            paths.add(entry[3:])
            if "R" in status_code or "C" in status_code:
                if index < len(entries) and entries[index]:
                    paths.add(entries[index])  # original path follows for renames/copies
                index += 1
        return sorted(paths)

    async def probe_worktree_status(self, worktree_path: Path, base_ref: str | None = None) -> WorktreeStatusProbe:
        """Probe the worktree state to verify commits, uncommitted changes, and active progress."""
        path = await self._validate_worktree(worktree_path)
        base = base_ref or await self.get_base_ref(path)
        head = await self._git(["rev-parse", "HEAD"], path)

        count_str = await self._git(["rev-list", "--count", f"{base}..HEAD"], path)
        try:
            commits_ahead = int(count_str.strip())
        except ValueError:
            commits_ahead = 0

        status_res = await run_async_command(["git", "status", "--porcelain=v1"], cwd=path)
        if not status_res.is_success:
            raise RuntimeError(f"Git status failed: {status_res.stderr.strip()}")
        status_raw = status_res.stdout
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []

        for line in status_raw.splitlines():
            if not line.strip():
                continue
            if line.startswith("??"):
                untracked.append(line[3:].strip())
            else:
                x = line[0] if len(line) > 0 else " "
                y = line[1] if len(line) > 1 else " "
                fname = line[2:].strip() if len(line) > 2 else ""
                if x not in (" ", "?"):
                    staged.append(fname)
                if y not in (" ", "?"):
                    unstaged.append(fname)

        is_clean = len(staged) == 0 and len(unstaged) == 0 and len(untracked) == 0
        has_meaningful_work = (
            commits_ahead > 0
            or len(staged) > 0
            or len(unstaged) > 0
            or any(f.endswith(".py") or f.endswith(".json") or f.endswith(".md") for f in untracked)
        )

        if commits_ahead > 0:
            summary = (
                f"Task produced {commits_ahead} commit(s) ahead of base ({head[:8]}); "
                f"working tree {'clean' if is_clean else 'dirty'}."
            )
        elif has_meaningful_work:
            summary = (
                f"No commits created yet, but working tree has active work "
                f"({len(staged)} staged, {len(unstaged)} unstaged, {len(untracked)} untracked files)."
            )
        else:
            summary = "No commits or file modifications detected in worktree."

        return WorktreeStatusProbe(
            worktree_path=path,
            base_ref=base,
            head_sha=head,
            commits_ahead=commits_ahead,
            is_clean=is_clean,
            staged_files=staged,
            unstaged_files=unstaged,
            untracked_files=untracked,
            has_meaningful_work=has_meaningful_work,
            summary=summary,
        )
