import re
from pathlib import Path

from execution.subproc import run_async_command


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

    async def get_diff(self, worktree_path: Path, base_ref: str | None = None) -> str:
        path = await self._validate_worktree(worktree_path)
        base = base_ref or await self.get_base_ref(path)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", base):
            raise ValueError("Diff requires a fixed base commit SHA")
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
