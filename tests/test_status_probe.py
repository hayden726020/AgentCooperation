import asyncio
from pathlib import Path
import pytest

from execution.worktree import WorktreeManager, WorktreeStatusProbe


@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    
    # Create initial commit
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=repo, check=True)
    return repo


@pytest.mark.asyncio
async def test_probe_worktree_status_clean_initial(temp_repo):
    wm = WorktreeManager(temp_repo)
    worktree = await wm.create_worktree("probe1")
    base_ref = await wm.get_base_ref(worktree)

    probe = await wm.probe_worktree_status(worktree, base_ref=base_ref)
    assert isinstance(probe, WorktreeStatusProbe)
    assert probe.base_ref == base_ref
    assert probe.commits_ahead == 0
    assert probe.is_clean is True
    assert probe.staged_files == []
    assert probe.unstaged_files == []
    assert probe.untracked_files == []
    assert probe.has_meaningful_work is False
    assert "No commits or file modifications detected" in probe.summary

    data = probe.to_dict()
    assert data["commits_ahead"] == 0
    assert data["is_clean"] is True
    assert data["has_meaningful_work"] is False


@pytest.mark.asyncio
async def test_probe_worktree_status_with_commits_ahead(temp_repo):
    wm = WorktreeManager(temp_repo)
    worktree = await wm.create_worktree("probe2")
    base_ref = await wm.get_base_ref(worktree)

    # Make a commit in worktree
    f = worktree / "hello.py"
    f.write_text("print('hello')", encoding="utf-8")
    await wm.commit_checkpoint(worktree, "feat: add hello.py")

    probe = await wm.probe_worktree_status(worktree, base_ref=base_ref)
    assert probe.commits_ahead == 1
    assert probe.is_clean is True
    assert probe.has_meaningful_work is True
    assert "Task produced 1 commit(s) ahead of base" in probe.summary


@pytest.mark.asyncio
async def test_probe_worktree_status_with_dirty_files(temp_repo):
    wm = WorktreeManager(temp_repo)
    worktree = await wm.create_worktree("probe3")
    base_ref = await wm.get_base_ref(worktree)

    # Add an untracked file and an unstaged modified file
    readme = worktree / "README.md"
    readme.write_text("# Test Repo Modified\n", encoding="utf-8")

    untracked = worktree / "scratch.py"
    untracked.write_text("x = 1\n", encoding="utf-8")

    probe = await wm.probe_worktree_status(worktree, base_ref=base_ref)
    assert probe.commits_ahead == 0
    assert probe.is_clean is False
    assert "README.md" in probe.unstaged_files
    assert "scratch.py" in probe.untracked_files
    assert probe.has_meaningful_work is True
    assert "active work" in probe.summary


@pytest.mark.asyncio
async def test_probe_worktree_status_omitted_base_and_staged(temp_repo):
    import subprocess
    wm = WorktreeManager(temp_repo)
    worktree = await wm.create_worktree("probe4")

    # Stage a file with git add
    staged_file = worktree / "staged.py"
    staged_file.write_text("a = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=worktree, check=True)

    # Call probe without base_ref (should auto-fetch orchestratorBase)
    probe = await wm.probe_worktree_status(worktree)
    assert probe.commits_ahead == 0
    assert probe.is_clean is False
    assert "staged.py" in probe.staged_files
    assert probe.unstaged_files == []
    assert probe.has_meaningful_work is True
    assert "1 staged" in probe.summary
