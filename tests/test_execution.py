import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from execution.subproc import SubprocResult, run_async_command
from execution.test_runner import TestRunner as Runner
from execution.worktree import WorktreeManager


def result(code=0, stdout="", stderr=""):
    return SubprocResult([], code, stdout, stderr, 0)


@pytest.mark.asyncio
async def test_worktree_creation_failure_is_not_a_fake_directory(tmp_path, monkeypatch):
    async def fake_git(cmd, **kwargs):
        return result(stdout="abc") if cmd[1] == "rev-parse" else result(1, stderr="failure")
    mock = AsyncMock(side_effect=fake_git)
    monkeypatch.setattr("execution.worktree.run_async_command", mock)
    manager = WorktreeManager(tmp_path)
    with pytest.raises(RuntimeError):
        await manager.create_worktree("safe")
    assert not (tmp_path / ".worktrees" / "safe").exists()


@pytest.mark.asyncio
async def test_empty_repo_is_not_automatically_committed(tmp_path, monkeypatch):
    mock = AsyncMock(return_value=result(1))
    monkeypatch.setattr("execution.worktree.run_async_command", mock)
    with pytest.raises(RuntimeError):
        await WorktreeManager(tmp_path).ensure_repo_has_initial_commit()
    assert mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["../escape", "a/b", "a\\b", "", "--force"])
async def test_invalid_task_id_rejected_before_git(tmp_path, monkeypatch, task_id):
    mock = AsyncMock(return_value=result())
    monkeypatch.setattr("execution.worktree.run_async_command", mock)
    with pytest.raises(ValueError):
        await WorktreeManager(tmp_path).create_worktree(task_id)
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_existing_fake_directory_rejected(tmp_path):
    path = tmp_path / ".worktrees" / "safe"
    path.mkdir(parents=True)
    with pytest.raises(ValueError):
        await WorktreeManager(tmp_path).create_worktree("safe")


@pytest.mark.asyncio
async def test_main_repository_rejected_for_checkpoint(tmp_path, monkeypatch):
    mock = AsyncMock(return_value=result())
    monkeypatch.setattr("execution.worktree.run_async_command", mock)
    with pytest.raises(ValueError):
        await WorktreeManager(tmp_path).commit_checkpoint(tmp_path, "unsafe")
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_pytest_statistics_are_real_counts(tmp_path, monkeypatch):
    (tmp_path / "test_solution.py").write_text("", encoding="utf-8")
    mock = AsyncMock(return_value=result(1, "=== 2 failed, 3 passed, 1 skipped in 0.1s ==="))
    monkeypatch.setattr("execution.test_runner.run_async_command", mock)
    outcome = await Runner(tmp_path).run()
    assert outcome.total_tests == 6
    assert outcome.failed_tests == 2
    assert not outcome.passed
    cmd = mock.call_args.kwargs["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "pytest"]
    assert "test_solution.py" in cmd


@pytest.mark.asyncio
async def test_zero_pytest_tests_cannot_pass(tmp_path, monkeypatch):
    (tmp_path / "test_solution.py").write_text("", encoding="utf-8")
    monkeypatch.setattr("execution.test_runner.run_async_command", AsyncMock(return_value=result(stdout="no tests ran in 0.1s")))
    outcome = await Runner(tmp_path).run()
    assert not outcome.passed
    assert outcome.total_tests == 0


@pytest.mark.asyncio
async def test_custom_unittest_command_with_zero_tests_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("execution.test_runner.run_async_command", AsyncMock(return_value=result(stdout="Ran 0 tests in 0.000s\nOK")))
    outcome = await Runner(tmp_path, custom_test_argv=[sys.executable, "-m", "unittest"]).run()
    assert not outcome.passed


def test_explicit_target_tests_do_not_collect_scheduler_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "test_solution.py").write_text("", encoding="utf-8")
    cmd = Runner(tmp_path, test_paths=["test_solution.py"]).detect_command()
    assert "test_solution.py" in cmd
    assert "tests" not in cmd


def test_custom_argv_preserves_quotes_and_spaces(tmp_path):
    argv = [sys.executable, "-c", "print('hello world')"]
    assert Runner(tmp_path, custom_test_argv=argv).detect_command() == argv


@pytest.mark.asyncio
async def test_subprocess_filters_inherited_secrets_but_allows_explicit_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    code = "import os;print(os.getenv('DEEPSEEK_API_KEY','absent'));print(os.getenv('EXPLICIT_TOKEN','absent'))"
    outcome = await run_async_command([sys.executable, "-c", code], env={"EXPLICIT_TOKEN": "supplied"})
    assert outcome.is_success
    assert outcome.stdout.splitlines() == ["absent", "supplied"]


@pytest.mark.asyncio
async def test_timeout_stops_process_promptly():
    outcome = await asyncio.wait_for(run_async_command([sys.executable, "-c", "import time;time.sleep(30)"], timeout=.1), timeout=5)
    assert outcome.timed_out
    assert not outcome.is_success


@pytest.mark.asyncio
async def test_cancellation_reaps_process(tmp_path):
    import psutil

    pid_path = tmp_path / "child.pid"
    code = "import os,pathlib,time;pathlib.Path(os.environ['PID_PATH']).write_text(str(os.getpid()));time.sleep(30)"
    task = asyncio.create_task(run_async_command([sys.executable, "-c", code], env={"PID_PATH": str(pid_path)}))
    async def wait_for_pid():
        while not pid_path.exists():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait_for_pid(), timeout=5)
    pid = int(pid_path.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not psutil.pid_exists(pid)


@pytest.mark.asyncio
async def test_commit_failure_cannot_return_successful_sha(tmp_path, monkeypatch):
    manager = WorktreeManager(tmp_path)
    path = tmp_path / ".worktrees" / "safe"
    monkeypatch.setattr(manager, "_validate_worktree", AsyncMock(return_value=path))
    async def fake_git(cmd, **kwargs):
        if cmd[1] == "diff":
            return result(1)
        if cmd[1] == "commit":
            return result(1, stderr="commit failed")
        return result(stdout="old-head")
    monkeypatch.setattr("execution.worktree.run_async_command", AsyncMock(side_effect=fake_git))
    with pytest.raises(RuntimeError, match="commit failed"):
        await manager.commit_checkpoint(path, "checkpoint")


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported(monkeypatch):
    def cannot_kill(pid):
        raise RuntimeError("cleanup denied")
    # A short-lived process avoids leaving a child running after the intentionally failed cleanup.
    monkeypatch.setattr("execution.subproc.kill_process_tree", cannot_kill)
    with pytest.raises(RuntimeError, match="cleanup denied"):
        await run_async_command([sys.executable, "-c", "import time;time.sleep(.1)"], timeout=.01)


@pytest.mark.asyncio
async def test_windows_custom_command_quotes_execute(tmp_path):
    test_file = tmp_path / "test target with spaces.py"
    test_file.write_text("def test_example():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q", str(test_file)])
    outcome = await Runner(tmp_path, custom_test_cmd=command).run()
    assert outcome.passed
    assert outcome.total_tests == 1


@pytest.mark.asyncio
async def test_real_git_worktree_checkpoints_and_fixed_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "initial.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "initial.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"], check=True, capture_output=True)
    manager = WorktreeManager(repo)
    worktree = await manager.create_worktree("safe")
    assert worktree.is_absolute()
    base = await manager.get_base_ref(worktree)
    assert await manager.commit_checkpoint(worktree, "no changes") == base
    (worktree / "new.txt").write_text("new", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.invalid"], check=True, capture_output=True)
    new = await manager.commit_checkpoint(worktree, "generated file")
    assert new != base
    assert "new.txt" in await manager.get_diff(worktree, base)
    await manager.cleanup_worktree("safe")
    assert not worktree.exists()
@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["1 skipped", "2 xfailed", "1 skipped, 1 xfailed"])
async def test_pytest_without_successful_tests_is_not_passed(tmp_path, monkeypatch, summary):
    import execution.test_runner as module
    from execution.subproc import SubprocResult
    (tmp_path / "test_empty.py").write_text("# target\n", encoding="utf-8")
    async def command(cmd, **kwargs):
        return SubprocResult(cmd, 0, summary, "", 0)
    monkeypatch.setattr(module, "run_async_command", command)
    result = await module.TestRunner(tmp_path).run()
    assert not result.passed


@pytest.mark.asyncio
async def test_unittest_all_skipped_is_not_passed(tmp_path, monkeypatch):
    import execution.test_runner as module
    from execution.subproc import SubprocResult
    async def command(cmd, **kwargs):
        return SubprocResult(cmd, 0, "Ran 2 tests in 0.001s\nOK (skipped=2)", "", 0)
    monkeypatch.setattr(module, "run_async_command", command)
    assert not (await module.TestRunner(tmp_path, custom_test_argv=["python", "-m", "unittest"]).run()).passed


def test_default_target_includes_existing_tests_directory(tmp_path):
    from execution.test_runner import TestRunner
    (tmp_path / "test_new.py").write_text("# target\n")
    (tmp_path / "tests").mkdir()
    assert "tests" in TestRunner(tmp_path).detect_command()


@pytest.mark.asyncio
async def test_custom_successful_command_without_test_evidence_is_rejected(tmp_path):
    outcome = await Runner(tmp_path, custom_test_argv=[sys.executable, "-c", "print('ok')"]).run()
    assert not outcome.passed
    assert outcome.total_tests == 0
    assert "Unsupported" in outcome.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("output, total, failed, passed", [
    ("echo complete", 0, 0, False),
    ("Tests: 2 passed, 2 total", 2, 0, True),
    ("Tests 1 failed | 3 passed (4)", 4, 1, False),
    ("  3 passing (10ms)\n  1 pending", 4, 0, True),
    ("  2 passing (10ms)\n  1 failing", 3, 1, False),
    ("# tests 3\n# pass 2\n# fail 0\n# skipped 1\n# todo 0", 3, 0, True),
    ("#tests 3\n#pass 2\n#fail 1", 3, 1, False),
    ("# tests 2\n# pass 0\n# fail 0\n# skipped 2", 2, 0, False),
])
async def test_custom_runner_requires_successful_test_counts(tmp_path, monkeypatch, output, total, failed, passed):
    monkeypatch.setattr("execution.test_runner.run_async_command", AsyncMock(return_value=result(stdout=output)))
    outcome = await Runner(tmp_path, custom_test_argv=["npm", "test"]).run()
    assert outcome.passed is passed
    assert outcome.total_tests == total
    assert outcome.failed_tests == failed


@pytest.mark.asyncio
async def test_unittest_expected_failures_are_not_successful_tests(tmp_path, monkeypatch):
    monkeypatch.setattr("execution.test_runner.run_async_command", AsyncMock(return_value=result(stdout="Ran 1 test in 0.001s\nOK (expected failures=1)")))
    outcome = await Runner(tmp_path, custom_test_argv=[sys.executable, "-m", "unittest"]).run()
    assert not outcome.passed
