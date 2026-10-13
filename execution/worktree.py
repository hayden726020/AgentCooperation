import asyncio
from pathlib import Path
from typing import Optional
from execution.subproc import run_async_command


class WorktreeManager:
    """
    负责各任务独立 Git Worktree 环境隔离与版本检查点控制。
    保证多任务互不干扰，支持自动回退破坏性修改。
    """

    def __init__(self, repo_root: Path, worktree_base_dir: str = ".worktrees"):
        self.repo_root = repo_root
        self.worktree_base = self.repo_root / worktree_base_dir
        self.worktree_base.mkdir(parents=True, exist_ok=True)

    async def ensure_repo_has_initial_commit(self):
        """确保主仓库至少有一次提交，否则无法创建 worktree"""
        check_head = await run_async_command(["git", "rev-parse", "--verify", "HEAD"], cwd=self.repo_root)
        if not check_head.is_success:
            await run_async_command(["git", "add", "."], cwd=self.repo_root)
            await run_async_command(
                ["git", "commit", "-m", "chore: initial commit for orchestrator"],
                cwd=self.repo_root
            )

    async def create_worktree(self, task_id: str) -> Path:
        """为当前任务创建专用的 worktree 和新分支"""
        await self.ensure_repo_has_initial_commit()
        branch_name = f"task/{task_id}"
        worktree_path = self.worktree_base / task_id

        if worktree_path.exists():
            return worktree_path

        # 尝试 git worktree add -b <branch> <path> HEAD
        res = await run_async_command(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
            cwd=self.repo_root
        )

        if not res.is_success:
            # 如果分支已存在，尝试不带 -b
            res2 = await run_async_command(
                ["git", "worktree", "add", str(worktree_path), branch_name],
                cwd=self.repo_root
            )
            if not res2.is_success:
                # 极端情况下如果 worktree 创建失败，直接回退为在 worktree_path 建立软拷贝目录
                worktree_path.mkdir(parents=True, exist_ok=True)
                return worktree_path

        return worktree_path

    async def commit_checkpoint(self, worktree_path: Path, message: str) -> Optional[str]:
        """为当前任务进展创建 git commit 检查点"""
        await run_async_command(["git", "add", "-A"], cwd=worktree_path)
        commit_res = await run_async_command(
            ["git", "commit", "-m", message],
            cwd=worktree_path
        )
        # 获取当前 HEAD sha
        sha_res = await run_async_command(["git", "rev-parse", "HEAD"], cwd=worktree_path)
        if sha_res.is_success:
            return sha_res.stdout.strip()
        return None

    async def get_diff(self, worktree_path: Path, base_ref: str = "HEAD~1") -> str:
        """获取与基础版本的 Diff 文本"""
        diff_res = await run_async_command(["git", "diff", base_ref], cwd=worktree_path)
        if diff_res.is_success and diff_res.stdout.strip():
            return diff_res.stdout.strip()
        
        # 如果 HEAD~1 不可用，获取当前已暂存/未暂存与 HEAD 的 diff
        diff_head = await run_async_command(["git", "diff", "HEAD"], cwd=worktree_path)
        return diff_head.stdout.strip()

    async def rollback(self, worktree_path: Path, target_sha: str) -> bool:
        """将工作区硬回退到指定检查点"""
        res = await run_async_command(["git", "reset", "--hard", target_sha], cwd=worktree_path)
        return res.is_success

    async def cleanup_worktree(self, task_id: str, force: bool = False):
        """清理 worktree 释放磁盘空间"""
        worktree_path = self.worktree_base / task_id
        cmd = ["git", "worktree", "remove", str(worktree_path)]
        if force:
            cmd.append("--force")
        await run_async_command(cmd, cwd=self.repo_root)
