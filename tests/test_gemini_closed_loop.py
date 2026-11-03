"""Offline contract and behavioral tests for Gemini Dev + GLM/DeepSeek Review closed-loop."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from execution.events import EventStore
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from agents.base import AgentResult
from agents.flash_reviewer import FlashReviewer
from agents.gemini_dev_agent import GeminiDevAgent
import sys
from config.settings import PROJECT_ROOT, ExecutionConfig, PolicyConfig, settings
from integrations.hermes.bridge import HermesCooperationBridge
from orchestrator.gemini_closed_loop import (
    ClosedLoopResult,
    DevelopmentArtifacts,
    GeminiClosedLoopOrchestrator,
    ReviewVerdict,
    redact_and_truncate_evidence,
    validate_custom_test_cmd,
)
from storage.models import ExecutionLog, Task


@pytest.mark.asyncio
@pytest.mark.parametrize("second_approved", [True, False])
async def test_approval_claim_prevents_competing_decisions(tmp_path, second_approved):
    """Separate database sessions cannot execute competing approval decisions."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'claims.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(Task(task_id="claim", title="claim", requirement="test", state="WAITING_APPROVAL"))
        setup.commit()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def blocked_impl(**kwargs):
        calls.append(kwargs["approved"])
        entered.set()
        await release.wait()
        return ClosedLoopResult(task_id="claim", success=False, state="FAILED", result_summary="done", repair_attempts=0)

    with Session(engine) as first_session, Session(engine) as second_session:
        first = GeminiClosedLoopOrchestrator(repo_root=tmp_path, session=first_session, event_store=EventStore(tmp_path / "events"))
        second = GeminiClosedLoopOrchestrator(repo_root=tmp_path, session=second_session, event_store=EventStore(tmp_path / "events"))
        first._resume_impl = blocked_impl
        second._resume_impl = blocked_impl
        running = asyncio.create_task(first.resume_after_approval("claim", True))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with pytest.raises(ValueError, match="WAITING_APPROVAL"):
                await asyncio.wait_for(second.resume_after_approval("claim", second_approved), 0.5)
        finally:
            release.set()
            await running
        assert calls == [True]


@pytest.mark.asyncio
async def test_cancelled_approval_releases_its_claim(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Task(task_id="cancel", title="cancel", requirement="test", state="WAITING_APPROVAL"))
        session.commit()
        orchestrator = GeminiClosedLoopOrchestrator(repo_root=tmp_path, session=session, event_store=EventStore(tmp_path / "events"))
        entered = asyncio.Event()

        async def blocked_impl(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        orchestrator._resume_impl = blocked_impl
        running = asyncio.create_task(orchestrator.resume_after_approval("cancel", True))
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        session.expire_all()
        task = session.exec(select(Task).where(Task.task_id == "cancel")).one()
        assert task.state == "WAITING_APPROVAL"
        assert task.approval_claim_id is None


@pytest.mark.asyncio
async def test_cancel_during_repair_requires_manual_review(mock_git_repo, test_db_session):
    orchestrator = GeminiClosedLoopOrchestrator(repo_root=mock_git_repo, session=test_db_session,
                                                event_store=EventStore(mock_git_repo.parent / "events"))
    test_db_session.add(Task(task_id="cancel_model", title="cancel", requirement="test",
                             state="WAITING_APPROVAL", repo_root=str(mock_git_repo)))
    test_db_session.commit()
    entered = asyncio.Event()

    async def executing_model(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    dev = SimpleNamespace(model_name="mock", execute=executing_model)
    running = asyncio.create_task(orchestrator.resume_after_approval("cancel_model", True, mock_mode=True, dev_agent=dev))
    await asyncio.wait_for(entered.wait(), 30)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    test_db_session.expire_all()
    task = test_db_session.exec(select(Task).where(Task.task_id == "cancel_model")).one()
    assert task.state == "INTERRUPTED"
    assert "inspect side effects and costs" in task.result_summary
    with pytest.raises(ValueError, match="WAITING_APPROVAL"):
        await orchestrator.resume_after_approval("cancel_model", True, mock_mode=True, dev_agent=dev)


def test_approval_restore_cannot_release_another_owner(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'owners.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Task(task_id="owned", title="owned", requirement="test", state="REPAIRING", approval_claim_id="new"))
        session.commit()
        orchestrator = GeminiClosedLoopOrchestrator(repo_root=tmp_path, session=session)
        assert orchestrator._restore_approval_state("owned", False, "old", claim_id="old") == "REPAIRING"
        orchestrator._release_approval_claim("owned", False, "old")
        session.expire_all()
        task = session.exec(select(Task).where(Task.task_id == "owned")).one()
        assert task.state == "REPAIRING"
        assert task.approval_claim_id == "new"


def test_approval_claim_across_independent_processes(tmp_path):
    """Two processes share one ledger; only one can enter the approval body."""
    import subprocess
    import time

    db_path = tmp_path / "processes.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Task(task_id="process", title="process", requirement="test", state="WAITING_APPROVAL"))
        session.commit()
    script = r'''
import asyncio, os, sys, time
from pathlib import Path
from sqlmodel import Session, create_engine
from orchestrator.gemini_closed_loop import GeminiClosedLoopOrchestrator, ClosedLoopResult
root = Path(sys.argv[1])
number = sys.argv[2]
engine = create_engine("sqlite:///" + (root / "processes.db").as_posix())
async def run():
    with Session(engine) as session:
        obj = GeminiClosedLoopOrchestrator(repo_root=root, session=session)
        obj._finalize_events = lambda **kwargs: None
        async def work(**kwargs):
            # Stand in for the first budget/model entry; no external call occurs.
            (root / ("budget-entry-" + number)).touch()
            while not (root / "release").exists():
                await asyncio.sleep(0.01)
            return ClosedLoopResult(task_id="process", success=False, state="FAILED", result_summary="offline", repair_attempts=0)
        obj._resume_impl = work
        (root / ("ready-" + number)).touch()
        while not (root / "go").exists():
            await asyncio.sleep(0.01)
        try:
            await obj.resume_after_approval("process", number == "0")
            print("won", flush=True)
        except ValueError:
            print("conflict", flush=True)
asyncio.run(run())
'''
    processes = [subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(i)],
        cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for i in range(2)]
    try:
        deadline = time.monotonic() + 30
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline and all(p.poll() is None for p in processes)
            time.sleep(0.02)
        (tmp_path / "go").touch()
        while all(p.poll() is None for p in processes):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert len(list(tmp_path.glob("budget-entry-*"))) == 1
    finally:
        (tmp_path / "release").touch()
        outputs = [p.communicate(timeout=15) for p in processes]
    assert all(p.returncode == 0 for p in processes), outputs
    assert sorted(out.strip() for out, _ in outputs) == ["conflict", "won"]


def test_concurrent_processes_migrate_approval_claim_column(tmp_path):
    import subprocess
    import time

    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE tasks DROP COLUMN approval_claim_id")
    script = r'''
import sys, time
from pathlib import Path
from sqlmodel import create_engine
from storage.db import init_db
root = Path(sys.argv[1]); number = sys.argv[2]
(root / ("ready-" + number)).touch()
while not (root / "go").exists(): time.sleep(0.01)
init_db(create_engine("sqlite:///" + (root / "migration.db").as_posix()))
'''
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
                                  cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]
    try:
        deadline = time.monotonic() + 30
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline and all(p.poll() is None for p in processes)
            time.sleep(0.02)
    finally:
        (tmp_path / "go").touch()
        outputs = [p.communicate(timeout=15) for p in processes]
    assert all(p.returncode == 0 for p in processes), outputs
    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(tasks)")}
    assert "approval_claim_id" in columns

HERMES_ROOT = Path(__file__).resolve().parents[1] / "hermes-agent"
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


@pytest.fixture
def test_db_session():
    """Isolated in-memory SQLite database session for closed-loop tests."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def mock_git_repo(tmp_path):
    """Create a minimal real Git repository with initial commit."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_closed_loop_success_first_attempt(mock_git_repo, test_db_session):
    """Test full closed-loop succeeds on the first attempt with no repairs needed."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=3,
    )

    # Mock dev agent writing a valid implementation
    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    result = await orchestrator.run(
        requirement="Implement multiply(a, b) in calc.py",
        mock_mode=True,
        dev_agent=dev_agent,
        reviewer_agent=reviewer_agent,
    )

    assert result.success is True
    assert result.state == "SUCCESS"
    assert result.repair_attempts == 0
    assert result.final_commit_sha is not None
    assert result.artifacts is not None
    assert result.artifacts.test_passed is True
    assert result.verdict is not None
    assert result.verdict.approved is True
    assert result.verdict.severity == "P3"

    # Verify task and execution logs persisted in DB
    task = test_db_session.exec(select(Task).where(Task.task_id == result.task_id)).first()
    assert task is not None
    assert task.state == "SUCCESS"
    assert task.primary_model == "gemini-3.8-flash-high"

    logs = test_db_session.exec(select(ExecutionLog).where(ExecutionLog.task_id == result.task_id)).all()
    roles = [log.agent_role for log in logs]
    assert "PRIMARY_DEVELOPER" in roles
    assert "REVIEWER" in roles


@pytest.mark.asyncio
async def test_closed_loop_retries_on_test_failure(mock_git_repo, test_db_session):
    """Test that a failing test on initial development triggers a repair retry and recovers."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=3,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    # First test run fails; second test run passes
    test_runs = [
        type("TestRes", (), {"passed": False, "raw_output": "AssertionError: 1 != 2", "summary": "1 failed"})(),
        type("TestRes", (), {"passed": True, "raw_output": "1 passed", "summary": "1 passed"})(),
    ]

    with patch("orchestrator.gemini_closed_loop.TestRunner.run", side_effect=test_runs):
        result = await orchestrator.run(
            requirement="Implement subtraction in calc.py",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.success is True
    assert result.state == "SUCCESS"
    assert result.repair_attempts == 1
    assert result.artifacts.test_passed is True
    assert result.verdict.approved is True


@pytest.mark.asyncio
async def test_closed_loop_retries_on_review_rejection(mock_git_repo, test_db_session):
    """Test that reviewer rejection (P2) triggers repair with suggestions and succeeds on retry."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="deepseek",
        max_repair_attempts=3,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="deepseek", mock_mode=True)

    # First review rejects with P2; second review approves
    rejections = [
        AgentResult(
            success=True,
            content="",
            metadata={
                "review": {
                    "approved": False,
                    "severity": "P2",
                    "summary": "Missing type annotations and docstring",
                    "critical_issues": ["No type annotations on public function"],
                    "suggestions": ["Add type hints `def sub(a: int, b: int) -> int`"],
                }
            },
        ),
        AgentResult(
            success=True,
            content="",
            metadata={
                "review": {
                    "approved": True,
                    "severity": "P3",
                    "summary": "Type hints added and verified",
                    "critical_issues": [],
                    "suggestions": [],
                }
            },
        ),
    ]

    with patch.object(reviewer_agent, "execute", side_effect=rejections):
        result = await orchestrator.run(
            requirement="Add typed sub function",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.success is True
    assert result.state == "SUCCESS"
    assert result.repair_attempts == 1
    assert result.verdict.approved is True
    assert len(result.history) == 2


@pytest.mark.asyncio
async def test_closed_loop_p0_halts_for_approval(mock_git_repo, test_db_session):
    """Test that a P0 critical security/vulnerability verdict halts automatically in WAITING_APPROVAL."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=3,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    p0_verdict = AgentResult(
        success=True,
        content="",
        metadata={
            "review": {
                "approved": False,
                "severity": "P0",
                "summary": "Potential remote code execution via unsafe eval()",
                "critical_issues": ["eval() used on unsanitized user input"],
                "suggestions": ["Replace eval with ast.literal_eval"],
            }
        },
    )

    with patch.object(reviewer_agent, "execute", return_value=p0_verdict):
        result = await orchestrator.run(
            requirement="Parse mathematical expressions",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.success is False
    assert result.state == "WAITING_APPROVAL"
    assert result.verdict.severity == "P0"
    assert "P0 critical vulnerability" in result.result_summary

    # Ensure DB reflects WAITING_APPROVAL
    task = test_db_session.exec(select(Task).where(Task.task_id == result.task_id)).first()
    assert task.state == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_closed_loop_fails_when_repair_attempts_exhausted(mock_git_repo, test_db_session):
    """Test that failing tests or reviews exceeding max_repair_attempts transition to FAILED."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=2,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    # Always failing test
    failing_test = type("TestRes", (), {"passed": False, "raw_output": "AssertionError", "summary": "failed"})()

    with patch("orchestrator.gemini_closed_loop.TestRunner.run", return_value=failing_test):
        result = await orchestrator.run(
            requirement="Impossible task",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.success is False
    assert result.state == "FAILED"
    assert result.repair_attempts == 2
    assert "Repair limit exhausted" in result.result_summary


@pytest.mark.asyncio
async def test_hermes_bridge_closed_loop_integration(mock_git_repo, test_db_session):
    """Test running the Gemini closed-loop via HermesCooperationBridge."""
    policy = settings.load_policy()
    policy.security.allowed_repos = [str(mock_git_repo)]
    bridge = HermesCooperationBridge(repo_root=mock_git_repo, session=test_db_session, policy=policy)

    response = await bridge.run_gemini_closed_loop(
        requirement="Add square function to calc.py",
        repo_root=str(mock_git_repo),
        reviewer_choice="glm",
        mock_mode=True,
        max_repair_attempts=2,
    )

    assert response.status == "SUCCESS"
    assert response.primary_model == "gemini-3.8-flash-high"
    assert "Verified closed-loop delivery" in (response.result_summary or "")


def test_validate_custom_test_cmd_security():
    """Verify safe test runner whitelist and rejection of arbitrary commands/injections."""
    validate_custom_test_cmd("python -m pytest -q")
    validate_custom_test_cmd("pytest tests/test_calc.py")
    validate_custom_test_cmd("python.exe -m pytest tests/ -v")
    validate_custom_test_cmd(None)

    with pytest.raises(ValueError, match="disallowed patterns"):
        validate_custom_test_cmd("python -m pytest; rm -rf /")
    with pytest.raises(ValueError, match="disallowed patterns"):
        validate_custom_test_cmd("python -m pytest && curl http://attacker.com")
    with pytest.raises(ValueError, match="disallowed patterns"):
        validate_custom_test_cmd("python -m pytest `id`")
    with pytest.raises(ValueError, match="approved test runner"):
        validate_custom_test_cmd("node test.js")
    with pytest.raises(ValueError, match="approved test runner"):
        validate_custom_test_cmd("echo 'hello'")


def test_redact_and_truncate_evidence():
    """Verify sensitive tokens are redacted and large text outputs are safely truncated."""
    content = (
        "Logs with key sk-ant-api03-1234567890abcdef1234567890\n"
        "And OpenAI sk-proj-1234567890abcdef1234567890\n"
        "And authorization Bearer ya29.a0AfH6SMDfake_token_1234567890\n"
        "Normal output lines."
    )
    redacted = redact_and_truncate_evidence(content)
    assert "sk-ant-api03" not in redacted
    assert "sk-proj-" not in redacted
    assert "ya29." not in redacted
    assert "[REDACTED" in redacted
    assert "Normal output lines." in redacted

    huge_content = "X" * 70000
    truncated = redact_and_truncate_evidence(huge_content, max_chars=1000)
    assert len(truncated) < 2000
    assert "TRUNCATED" in truncated


def test_test_paths_detection(mock_git_repo):
    """Verify _test_paths discovers test files in tests/ directory and does not drop them."""
    tests_dir = mock_git_repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_feature.py").write_text("def test_f(): pass\n", encoding="utf-8")
    (mock_git_repo / "test_root.py").write_text("def test_r(): pass\n", encoding="utf-8")

    paths = GeminiClosedLoopOrchestrator._test_paths(mock_git_repo, ["calc.py", "test_root.py"])
    assert "tests/test_feature.py" in paths
    assert "test_root.py" in paths


def test_nested_sensitive_paths_protection():
    """Verify arbitrary nested sensitive directories and credentials are intercepted."""
    import importlib.util
    mode_path = Path(__file__).resolve().parents[1] / "hermes-agent/plugins/model-providers/antigravity-cli/project_mode.py"
    spec = importlib.util.spec_from_file_location("project_mode_test", mode_path)
    proj_mode = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = proj_mode
    spec.loader.exec_module(proj_mode)
    is_protected_relative_path = proj_mode.is_protected_relative_path

    assert is_protected_relative_path(Path(".env"))
    assert is_protected_relative_path(Path("sub/.env"))
    assert is_protected_relative_path(Path("sub/dir/.env.production"))
    assert is_protected_relative_path(Path(".git/config"))
    assert is_protected_relative_path(Path("nested/.git/HEAD"))
    assert is_protected_relative_path(Path(".aws/credentials"))
    assert is_protected_relative_path(Path("nested/.codex/config.json"))
    assert is_protected_relative_path(Path("auth.json"))
    assert is_protected_relative_path(Path("config/token.json"))
    assert is_protected_relative_path(Path("certs/server.pem"))
    assert is_protected_relative_path(Path("keys/id_rsa"))

    # Benign files should pass
    assert not is_protected_relative_path(Path("src/main.py"))
    assert not is_protected_relative_path(Path("tests/test_app.py"))
    assert not is_protected_relative_path(Path("README.md"))


def test_gemini_dev_agent_contract():
    """Verify GeminiDevAgent loads default model and metadata structure."""
    agent = GeminiDevAgent(mock_mode=True)
    assert agent.model_name == "gemini-3.8-flash-high"
    assert agent.role_name == "PRIMARY_DEVELOPER"
    assert agent.timeout == 900.0


def test_closed_loop_uses_policy_model_timeout(mock_git_repo, test_db_session):
    """The real Gemini developer receives the bounded timeout from policy."""
    policy = PolicyConfig(execution={"model_timeout_seconds": 1200})
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        policy=policy,
        reviewer_choice="glm",
    )

    agent = orchestrator._new_dev_agent(mock_mode=False)

    assert agent.timeout == 1200.0


def test_loaded_policy_allows_long_running_gemini_development():
    """The checked-in production policy must exceed the old five-minute limit."""
    policy = settings.load_policy()

    assert policy.execution.model_timeout_seconds == 900


@pytest.mark.parametrize("timeout", [59, 1801])
def test_model_timeout_policy_is_bounded(timeout):
    """Policy must reject both ineffective and effectively unbounded model runs."""
    with pytest.raises(ValueError):
        PolicyConfig(execution={"model_timeout_seconds": timeout})


@pytest.mark.asyncio
async def test_closed_loop_gate_rejects_empty_changes(mock_git_repo, test_db_session):
    """Verify Gate 1 and Gate 2 reject executions where Gemini produces zero writes or empty diff."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )

    empty_dev_agent = GeminiDevAgent(mock_mode=True)

    async def mock_execute_no_write(prompt, context=None):
        return AgentResult(
            success=True,
            content="I looked at the code but made no modifications.",
            metadata={"files_written": [], "native_tool_steps": [{"name": "view_file", "succeeded": True}]},
        )

    with patch.object(empty_dev_agent, "execute", side_effect=mock_execute_no_write):
        result = await orchestrator.run(
            requirement="Check code and do nothing",
            mock_mode=False,
            dev_agent=empty_dev_agent,
        )

    assert result.success is False
    assert result.state == "FAILED"
    assert "without executing any successful native file modification tools" in result.result_summary


@pytest.mark.asyncio
async def test_closed_loop_gate_rejects_dirty_worktree_after_test(mock_git_repo, test_db_session):
    """Verify Gate 3 rejects tasks where test execution leaves uncommitted modifications in the worktree."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    async def dirtying_test_run(*args, **kwargs):
        orchestrator.worktree_mgr.worktree_base.mkdir(parents=True, exist_ok=True)
        for wt in orchestrator.worktree_mgr.worktree_base.iterdir():
            if wt.is_dir():
                (wt / "dirty_polluted.txt").write_text("polluted by rogue test", encoding="utf-8")
        return type("TestRes", (), {"passed": True, "raw_output": "passed", "summary": "1 passed"})()

    with patch("orchestrator.gemini_closed_loop.TestRunner.run", side_effect=dirtying_test_run):
        result = await orchestrator.run(
            requirement="Run test that mutates workspace",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.success is False
    assert result.state == "FAILED"
    assert "Working directory state modified during test execution" in result.result_summary


@pytest.mark.asyncio
async def test_closed_loop_p0_persistence_and_approval_resume(mock_git_repo, test_db_session, tmp_path):
    """Test full cycle: P0 halts in WAITING_APPROVAL -> resume with human approval -> Gemini repairs and succeeds."""
    store = EventStore(tmp_path / "events")
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=3,
        event_store=store,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    p0_verdict = AgentResult(
        success=True,
        content="",
        metadata={
            "review": {
                "approved": False,
                "severity": "P0",
                "summary": "Critical vulnerability detected in calc.py",
                "critical_issues": ["eval() vulnerability"],
                "suggestions": ["Replace with safe parser"],
            }
        },
    )

    with patch.object(reviewer_agent, "execute", return_value=p0_verdict):
        result = await orchestrator.run(
            requirement="Security critical calculation",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.state == "WAITING_APPROVAL"
    task_id = result.task_id

    paused_result = store.read(task_id)[-1]
    assert paused_result.type == "result"
    assert paused_result.payload["terminal"] is False
    assert paused_result.is_terminal is False

    # 1. Test human rejection transitions to FAILED
    reject_res = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=False,
        notes="Do not proceed with unsafe design",
        mock_mode=True,
    )
    assert reject_res.success is False
    assert reject_res.state == "FAILED"
    assert "Rejected by human operator" in reject_res.result_summary

    # Reset task state to WAITING_APPROVAL for approved path
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        task.state = "WAITING_APPROVAL"
        s.add(task)
        s.commit()

    # 2. Test human approval: Gemini repairs and reviewer approves
    approved_verdict = AgentResult(
        success=True,
        content="",
        metadata={
            "review": {
                "approved": True,
                "severity": "P3",
                "summary": "Safe parser verified",
                "critical_issues": [],
                "suggestions": [],
            }
        },
    )

    with patch.object(reviewer_agent, "execute", return_value=approved_verdict):
        resume_res = await orchestrator.resume_after_approval(
            task_id=task_id,
            approved=True,
            notes="Authorized: apply safe parser replacement",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert resume_res.success is True
    assert resume_res.state == "SUCCESS"
    assert resume_res.repair_attempts >= 1
    assert "Verified closed-loop delivery after human approval" in resume_res.result_summary

    resumed_result = store.read(task_id)[-1]
    assert resumed_result.type == "result"
    assert resumed_result.payload["terminal"] is True
    assert resumed_result.is_terminal is True


@pytest.mark.asyncio
async def test_approved_repair_blocks_untrusted_test_code(mock_git_repo, test_db_session):
    """Approved repairs must still apply the untrusted-test gate before execution."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_untrusted_repair"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="untrusted repair",
            requirement="Repair with a new test",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    async def fake_execute(prompt, context=None):
        return AgentResult(
            success=True,
            content="",
            metadata={
                "files_written": ["tests/test_evil.py"],
                "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
            },
        )

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=fake_execute)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        notes="Apply the fix",
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "FAILED"
    assert "Untrusted test code" in result.result_summary

    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "FAILED"


@pytest.mark.asyncio
async def test_approved_repair_blocks_shell_written_tests(mock_git_repo, test_db_session):
    """Tests created through shell commands must still trip the untrusted gate."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_shell_tests"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="shell written tests",
            requirement="Repair",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    async def fake_execute(prompt, context=None):
        worktree = Path(context["worktree_path"])
        (worktree / "tests").mkdir(exist_ok=True)
        (worktree / "tests" / "test_shell.py").write_text(
            "def test_shell():\n    assert True\n", encoding="utf-8"
        )
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# repaired\n", encoding="utf-8"
        )
        return AgentResult(
            success=True,
            content="",
            metadata={
                "files_written": ["calc.py"],
                "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
            },
        )

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=fake_execute)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        notes="Apply the fix",
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "FAILED"
    assert "Untrusted test code" in result.result_summary
    assert "tests/test_shell.py" in result.result_summary


@pytest.mark.asyncio
async def test_resume_budget_block_keeps_task_waiting(mock_git_repo, test_db_session, tmp_path):
    """A live budget pre-flight must leave the paused task retryable."""
    store = EventStore(tmp_path / "events")
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        event_store=store,
    )
    task_id = "task_budget_pause"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="budget pause",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    orchestrator.budget_guard.check_can_start_new_task = lambda: (False, "Monthly budget exhausted")

    result = await orchestrator.resume_after_approval(
        task_id=task_id, approved=True, mock_mode=False,
    )

    assert result.success is False
    assert result.state == "WAITING_APPROVAL"
    assert "Monthly budget exhausted" in result.result_summary
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "WAITING_APPROVAL"
    last = store.read(task_id)[-1]
    assert last.type == "result"
    assert last.payload["terminal"] is False


@pytest.mark.asyncio
async def test_resume_failure_requires_review_after_execution_starts(mock_git_repo, test_db_session, tmp_path):
    """A worktree operation may have side effects even if it raises."""
    store = EventStore(tmp_path / "events")
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        event_store=store,
    )
    task_id = "task_resume_failure"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="resume failure",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("worktree unavailable")

    with patch.object(orchestrator.worktree_mgr, "create_worktree", side_effect=boom):
        with pytest.raises(RuntimeError, match="worktree unavailable"):
            await orchestrator.resume_after_approval(
                task_id=task_id, approved=True, mock_mode=False,
            )

    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "INTERRUPTED"
    last = store.read(task_id)[-1]
    assert last.type == "result"
    assert last.payload["terminal"] is True


@pytest.mark.parametrize("path", [
    "conftest.py",
    "tests/conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "Tests/helpers.py",
    "TEST_calc.py",
])
def test_untrusted_detector_includes_pytest_config_and_case_variants(path):
    """Pytest hooks and config execute code, so they must trip the same gate as tests."""
    assert GeminiClosedLoopOrchestrator._detect_modified_or_new_tests(
        ["calc.py", path]
    ) == [path]


@pytest.mark.asyncio
async def test_resume_blocks_tests_committed_in_paused_segment(mock_git_repo, test_db_session):
    """Tests committed before the pause must not execute on a resume without opt-in."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_prior_untrusted"
    worktree = await orchestrator.worktree_mgr.create_worktree(task_id)
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_prior.py").write_text(
        "def test_prior():\n    assert True\n", encoding="utf-8"
    )
    (worktree / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n# first segment\n", encoding="utf-8"
    )
    await orchestrator.worktree_mgr.commit_checkpoint(worktree, "feat: paused segment")

    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="prior untrusted",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            worktree_path=str(worktree),
            reviewer_model="glm",
        ))
        s.commit()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("dev agent must not run before the prior-test gate")

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=must_not_run)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        notes="Apply the fix",
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "WAITING_APPROVAL"
    assert "tests/test_prior.py" in result.result_summary
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_resume_blocks_test_committed_by_dev_during_repair(mock_git_repo, test_db_session):
    """A dev call that commits its own test file must not bypass the gate via clean status."""
    import subprocess

    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_sneaky_commit"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="sneaky commit",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    async def fake_execute(prompt, context=None):
        worktree = Path(context["worktree_path"])
        (worktree / "tests").mkdir(exist_ok=True)
        (worktree / "tests" / "test_sneaky.py").write_text(
            "def test_sneaky():\n    assert True\n", encoding="utf-8"
        )
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# repair\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "sneaky repair"], cwd=worktree, check=True, capture_output=True
        )
        return AgentResult(
            success=True,
            content="",
            metadata={
                "files_written": ["calc.py"],
                "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
            },
        )

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=fake_execute)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "FAILED"
    assert "tests/test_sneaky.py" in result.result_summary


@pytest.mark.asyncio
async def test_resume_blocks_gitignored_test_written_by_dev(mock_git_repo, test_db_session):
    """Ignored test files must trip the gate even though git status hides them by default."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_ignored_test"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="ignored test",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    async def fake_execute(prompt, context=None):
        worktree = Path(context["worktree_path"])
        (worktree / ".gitignore").write_text("tests/\n", encoding="utf-8")
        (worktree / "tests").mkdir(exist_ok=True)
        (worktree / "tests" / "test_ignored.py").write_text(
            "def test_ignored():\n    assert True\n", encoding="utf-8"
        )
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# repair\n", encoding="utf-8"
        )
        return AgentResult(
            success=True,
            content="",
            metadata={
                "files_written": ["calc.py"],
                "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
            },
        )

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=fake_execute)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "FAILED"
    assert "tests/" in result.result_summary


@pytest.mark.asyncio
async def test_normal_repair_blocks_test_committed_by_dev(mock_git_repo, test_db_session):
    """The first-pass repair loop must also detect tests committed by run_command."""
    import subprocess

    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=2,
    )
    calls = {"dev": 0}

    async def fake_dev(prompt, context=None):
        calls["dev"] += 1
        worktree = Path(context["worktree_path"])
        if calls["dev"] == 1:
            (worktree / "calc.py").write_text(
                "def add(a, b):\n    return a + b + 0\n", encoding="utf-8"
            )
            return AgentResult(
                success=True,
                content="",
                metadata={
                    "files_written": ["calc.py"],
                    "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
                },
            )
        (worktree / "tests").mkdir(exist_ok=True)
        (worktree / "tests" / "test_evil.py").write_text(
            "def test_evil():\n    assert True\n", encoding="utf-8"
        )
        (worktree / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n# repaired\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "sneaky repair"], cwd=worktree, check=True, capture_output=True
        )
        return AgentResult(
            success=True,
            content="",
            metadata={
                "files_written": ["calc.py"],
                "native_tool_steps": [{"name": "write_to_file", "succeeded": True, "parameters": {}}],
            },
        )

    async def rejecting_review(prompt, context=None):
        return AgentResult(
            success=True,
            content="",
            metadata={
                "review": {
                    "approved": False,
                    "severity": "P1",
                    "summary": "needs repair",
                    "critical_issues": ["style"],
                    "suggestions": [],
                }
            },
        )

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=fake_dev)
    reviewer_agent = SimpleNamespace(
        model_name="glm-5.3-flashx", model_key="glm", execute=rejecting_review,
    )

    result = await orchestrator.run(
        requirement="Repair safely",
        mock_mode=False,
        dev_agent=dev_agent,
        reviewer_agent=reviewer_agent,
    )

    assert calls["dev"] == 2, (result.state, result.result_summary)
    assert result.success is False
    assert result.state == "FAILED"
    assert "tests/test_evil.py" in result.result_summary


@pytest.mark.asyncio
async def test_worktree_changed_paths_reports_rename_origin(mock_git_repo):
    """Renaming a test out of the test tree must still expose the original path."""
    import subprocess

    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=None,
        reviewer_choice="glm",
    )
    worktree = await orchestrator.worktree_mgr.create_worktree("task_rename_paths")
    subprocess.run(
        ["git", "mv", "test_calc.py", "calc_helpers.py"],
        cwd=worktree, check=True, capture_output=True,
    )

    paths = await orchestrator.worktree_mgr.get_changed_paths(worktree)

    assert "test_calc.py" in paths
    assert "calc_helpers.py" in paths


@pytest.mark.asyncio
async def test_resume_blocks_prior_ignored_test(mock_git_repo, test_db_session):
    """An ignored, uncommitted test left by a paused segment must still gate the resume."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_prior_ignored"
    worktree = await orchestrator.worktree_mgr.create_worktree(task_id)
    (worktree / ".gitignore").write_text("tests/\n", encoding="utf-8")
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_prior_ignored.py").write_text(
        "def test_prior_ignored():\n    assert True\n", encoding="utf-8"
    )
    await orchestrator.worktree_mgr.commit_checkpoint(worktree, "feat: pause")

    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="prior ignored",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            worktree_path=str(worktree),
            reviewer_model="glm",
        ))
        s.commit()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("dev agent must not run before the prior-test gate")

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=must_not_run)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.state == "WAITING_APPROVAL"
    assert "tests/" in result.result_summary


@pytest.mark.asyncio
async def test_resume_blocks_prior_test_deletion(mock_git_repo, test_db_session):
    """Deleting a test in a paused segment must not silently weaken the suite."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_deleted_test"
    worktree = await orchestrator.worktree_mgr.create_worktree(task_id)
    (worktree / "test_calc.py").unlink()
    await orchestrator.worktree_mgr.commit_checkpoint(worktree, "chore: remove test")

    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="deleted test",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            worktree_path=str(worktree),
            reviewer_model="glm",
        ))
        s.commit()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("dev agent must not run before the prior-test gate")

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=must_not_run)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.state == "WAITING_APPROVAL"
    assert "test_calc.py" in result.result_summary


@pytest.mark.asyncio
async def test_stale_approval_does_not_rewind_live_run(mock_git_repo, test_db_session):
    """A duplicate approval must not reset a run another caller is already resuming."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_live_resume"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="live resume",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="REPAIRING",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    with pytest.raises(ValueError, match="expected 'WAITING_APPROVAL'"):
        await orchestrator.resume_after_approval(
            task_id=task_id, approved=True, mock_mode=False,
        )

    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "REPAIRING"


@pytest.mark.asyncio
async def test_resume_refuses_when_repair_limit_exhausted(mock_git_repo, test_db_session):
    """Approval must not launch an over-limit paid repair attempt."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_limit_exhausted"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="limit exhausted",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
            max_repair_attempts=0,
        ))
        s.commit()

    async def must_not_run(*args, **kwargs):
        raise AssertionError("repair must not run past the configured limit")

    dev_agent = SimpleNamespace(model_name="gemini-3.8-flash-high", execute=must_not_run)

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=True,
        mock_mode=False,
        dev_agent=dev_agent,
    )

    assert result.success is False
    assert result.state == "FAILED"
    assert "Repair limit already exhausted" in result.result_summary
    assert result.repair_attempts == 0


@pytest.mark.asyncio
async def test_rejection_ignores_invalid_test_paths_and_stale_repo(mock_git_repo, test_db_session, tmp_path):
    """A rejection must be able to close a run whose repo or test paths no longer resolve."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = "task_reject_stale"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="stale reject",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            repo_root=str(tmp_path / "repo-moved-away"),
            reviewer_model="glm",
            test_paths='["../escape.py"]',
        ))
        s.commit()

    result = await orchestrator.resume_after_approval(
        task_id=task_id,
        approved=False,
        notes="abandon",
        mock_mode=False,
    )

    assert result.state == "FAILED"
    assert "Rejected by human operator" in result.result_summary
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["REPAIRING", "TESTING", "REVIEWING"])
async def test_interrupted_resume_states_are_restored(mock_git_repo, test_db_session, state):
    """Any mid-flight resume state must return to WAITING_APPROVAL after a crash."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    task_id = f"task_restore_{state.lower()}"
    with test_db_session as s:
        s.add(Task(
            task_id=task_id,
            title="restore",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state=state,
            approval_claim_id="owned",
            repo_root=str(mock_git_repo.resolve()),
            reviewer_model="glm",
        ))
        s.commit()

    restored = orchestrator._restore_approval_state(task_id, False, "interrupted", claim_id="owned")

    assert restored == "WAITING_APPROVAL"
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == task_id)).first()
        assert task.state == "WAITING_APPROVAL"


def test_resume_seeds_usage_totals_from_task(mock_git_repo, test_db_session):
    """A resumed segment reports prompt/completion/cost cumulative across segments."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )
    with test_db_session as s:
        s.add(Task(
            task_id="task_usage_seed",
            title="seed usage",
            requirement="r",
            primary_model="gemini-3.8-flash-high",
            state="WAITING_APPROVAL",
            total_prompt_tokens=120,
            total_completion_tokens=45,
            total_cost_cny=0.25,
        ))
        s.commit()

    orchestrator._usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0}
    orchestrator._seed_usage_from_task("task_usage_seed", mock_mode=False)
    assert orchestrator._usage_total == {
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "cost_cny": 0.25,
    }


@pytest.mark.asyncio
async def test_reviewer_usage_is_not_double_counted(mock_git_repo, test_db_session):
    """BudgetGuard settlement and event instrumentation must not both add reviewer usage."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=1,
    )
    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)
    verdict = AgentResult(
        success=True,
        content="",
        prompt_tokens=100,
        completion_tokens=50,
        cost_cny=0.1,
        duration_s=0.5,
        metadata={
            "review": {
                "approved": True,
                "severity": "P3",
                "summary": "Approved",
                "critical_issues": [],
                "suggestions": [],
            }
        },
    )

    with patch.object(reviewer_agent, "execute", return_value=verdict):
        result = await orchestrator.run(
            requirement="Usage accounting",
            mock_mode=True,
            dev_agent=dev_agent,
            reviewer_agent=reviewer_agent,
        )

    assert result.state == "SUCCESS"
    with test_db_session as s:
        task = s.exec(select(Task).where(Task.task_id == result.task_id)).first()
        assert task.total_prompt_tokens == 100
        assert task.total_completion_tokens == 50
        assert task.total_cost_cny == pytest.approx(0.1)


def test_hermes_tool_registration_and_mcp_dispatch():
    """Verify closed-loop tool schema is registered and can be called via MCP server and plugin."""
    from plugins.agent_cooperation.tools import RUN_GEMINI_CLOSED_LOOP_SCHEMA
    from integrations.hermes.mcp_server import TOOLS, handle_tool_call

    assert RUN_GEMINI_CLOSED_LOOP_SCHEMA["name"] == "coop_run_gemini_closed_loop"

    tool_names = [t["name"] for t in TOOLS]
    assert "coop_run_gemini_closed_loop" in tool_names


@pytest.mark.asyncio
async def test_mock_vs_real_db_isolation(mock_git_repo, tmp_path):
    """Verify mock closed-loop executions write to mock DB and never contaminate the real DB."""
    from config.settings import PROJECT_ROOT
    mock_db_file = PROJECT_ROOT / "data/mock_agent_orchestrator.db"
    real_db_file = Path(settings.sqlite_db_path)

    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=None,
        reviewer_choice="glm",
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    result = await orchestrator.run(
        requirement="Isolated mock test",
        mock_mode=True,
        dev_agent=dev_agent,
        reviewer_agent=reviewer_agent,
    )

    assert result.success is True
    assert mock_db_file.exists()

    from storage.db import get_engine
    mock_engine = get_engine(str(mock_db_file))
    with Session(mock_engine) as s:
        task = s.exec(select(Task).where(Task.task_id == result.task_id)).first()
        assert task is not None
        assert task.mock_mode is True

    real_engine = get_engine(str(real_db_file))
    with Session(real_engine) as rs:
        task_real = rs.exec(select(Task).where(Task.task_id == result.task_id)).first()
        assert task_real is None


def test_safe_test_path_validation_and_attack_rejection(mock_git_repo):
    """Test safe test path validation rejects traversals, flags, shell chars, and symlinks."""
    from orchestrator.gemini_closed_loop import validate_safe_test_paths

    # Valid relative path exists
    assert validate_safe_test_paths(["test_calc.py"], mock_git_repo) == ["test_calc.py"]

    # Rejects path traversal
    with pytest.raises(ValueError, match="Path traversal"):
        validate_safe_test_paths(["../outside.py"], mock_git_repo)

    # Rejects absolute paths
    with pytest.raises(ValueError, match="Absolute test paths disallowed"):
        validate_safe_test_paths(["/etc/passwd"], mock_git_repo)
    with pytest.raises(ValueError, match="Absolute test paths disallowed"):
        validate_safe_test_paths(["C:/Windows/calc.exe"], mock_git_repo)

    # Rejects flag options
    with pytest.raises(ValueError, match="Flag arguments not permitted"):
        validate_safe_test_paths(["-p"], mock_git_repo)
    with pytest.raises(ValueError, match="Flag arguments not permitted"):
        validate_safe_test_paths(["--rootdir=.."], mock_git_repo)

    # Rejects shell metacharacters
    for bad_char in [";", "&", "|", "`", "$", "<", ">", "\n"]:
        with pytest.raises(ValueError, match="Disallowed character"):
            validate_safe_test_paths([f"test_calc.py{bad_char}id"], mock_git_repo)

    # Rejects nonexistent files when worktree is provided
    with pytest.raises(ValueError, match="Test path does not exist"):
        validate_safe_test_paths(["test_nonexistent.py"], mock_git_repo)


def test_test_runner_hardened_env(mock_git_repo, monkeypatch):
    """Verify subprocess environment cleans credentials, sets sandbox dirs and disables plugins."""
    from execution.test_runner import TestRunner

    # Inject mock sensitive credentials into current environment
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-12345")
    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_1234567890abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8080")

    runner = TestRunner(mock_git_repo, test_paths=["test_calc.py"])
    env = runner.build_env(mock_git_repo)

    # Credential stripping
    for key in ("OPENAI_API_KEY", "ZHIPU_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
        assert key not in env

    # Proxy isolation
    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env

    # Sandbox isolation
    assert env.get("PYTHONNOUSERSITE") == "1"
    assert env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    assert ".test_sandbox" in env.get("TEMP", "")
    assert ".test_sandbox" in env.get("USERPROFILE", "")


@pytest.mark.asyncio
async def test_untrusted_test_approval_gate(mock_git_repo, test_db_session):
    """Test that newly generated test files trigger WAITING_APPROVAL when allow_untrusted_tests is False."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    async def fake_dev_execute(prompt, context=None):
        wt = Path(context["worktree_path"])
        test_file = wt / "test_untrusted.py"
        test_file.write_text("def test_foo(): assert True\n", encoding="utf-8")
        return AgentResult(
            success=True,
            content="Added test_untrusted.py",
            metadata={"files_written": [str(test_file)], "native_tool_steps": [{"name": "write_to_file", "succeeded": True}]},
        )

    with patch.object(dev_agent, "execute", side_effect=fake_dev_execute):
        result = await orchestrator.run(
            requirement="Add new feature with test",
            mock_mode=False,
            dev_agent=dev_agent,
            allow_untrusted_tests=False,
        )

    assert result.success is False
    assert result.state == "WAITING_APPROVAL"
    assert "Untrusted test code generated or modified" in result.result_summary


@pytest.mark.asyncio
async def test_sensitive_file_modification_aborts_task(mock_git_repo, test_db_session):
    """Test that writing to sensitive files like .env or keys immediately halts execution."""
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    async def fake_sensitive_execute(prompt, context=None):
        wt = Path(context["worktree_path"])
        env_file = wt / ".env"
        env_file.write_text("SECRET=bad\n", encoding="utf-8")
        return AgentResult(
            success=True,
            content="Touched .env",
            metadata={"files_written": [str(env_file)], "native_tool_steps": [{"name": "write_to_file", "succeeded": True}]},
        )

    with patch.object(dev_agent, "execute", side_effect=fake_sensitive_execute):
        result = await orchestrator.run(
            requirement="Modify secret configs",
            mock_mode=False,
            dev_agent=dev_agent,
        )

    assert result.success is False
    assert result.state == "FAILED"
    assert "Security violation" in result.result_summary


def test_advanced_evidence_redaction():
    """Verify AWS, GitHub tokens, PEM keys, and DB URIs are redacted."""
    raw = (
        "AWS Key: AKIAIOSFODNN7EXAMPLE\n"
        "GitHub PAT: ghp_1234567890abcdefghijklmnopqrstuvwxyz1234\n"
        "GitHub Fine-grained: github_pat_11AEXAMPLE0123456789abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrstuv\n"
        "Postgres: postgres://dbuser:supersecretpass@db.internal:5432/main\n"
        "Key: -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0\n-----END RSA PRIVATE KEY-----\n"
    )
    redacted = redact_and_truncate_evidence(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED_AWS_KEY]" in redacted
    assert "ghp_1234567890" not in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted
    assert "supersecretpass" not in redacted
    assert "[REDACTED_DB_CREDENTIALS]" in redacted
    assert "MIIEow" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


@pytest.mark.asyncio
async def test_hermes_slash_command_and_cli_dispatch(mock_git_repo, test_db_session):
    """Verify /coop loop and MCP invoke bridge and closed loop orchestrator."""
    from plugins.agent_cooperation.commands import handle_coop_command
    from plugins.agent_cooperation.tools import get_bridge
    from integrations.hermes.mcp_server import handle_tool_call
    import integrations.hermes.mcp_server as mcp_mod

    bridge = get_bridge()
    bridge.session = test_db_session
    bridge.policy.security.allowed_repos = [str(mock_git_repo)]
    mcp_mod.BRIDGE = bridge

    # 1. Test /coop loop
    repo_arg = mock_git_repo.as_posix()
    cmd_output = await handle_coop_command(f'loop "Add mock function" --mock --repo "{repo_arg}"')
    assert "Task:" in cmd_output
    assert "SUCCESS" in cmd_output

    # 2. Test MCP tool dispatch
    mcp_res = await handle_tool_call(
        "coop_run_gemini_closed_loop",
        {"requirement": "Add test feature via MCP", "mock_mode": True, "repo_root": str(mock_git_repo)},
    )
    assert mcp_res["status"] == "SUCCESS"
    assert "task_id" in mcp_res


def test_hermes_coop_cli_dispatch(mock_git_repo):
    """Verify hermes coop loop CLI parser & dispatch executes cleanly in synchronous context."""
    from plugins.agent_cooperation.cli import register_cli
    from plugins.agent_cooperation.tools import get_bridge
    import argparse

    bridge = get_bridge()
    bridge.session = None
    bridge.policy.security.allowed_repos = [str(mock_git_repo)]

    parser = argparse.ArgumentParser(prog="hermes coop")
    register_cli(parser)
    parsed_args = parser.parse_args(["loop", "Add test via CLI", "--mock", "--repo", str(mock_git_repo)])
    assert parsed_args.coop_action == "loop"

    bare_args = parser.parse_args(["Add test via CLI", "--mock", "--repo", str(mock_git_repo)])
    assert bare_args.coop_action == "loop"
    assert bare_args.requirement == ["Add test via CLI"]

    multi_word_args = parser.parse_args(["修复", "登录", "页面", "--mock"])
    assert multi_word_args.coop_action == "loop"
    assert multi_word_args.requirement == ["修复", "登录", "页面"]
    cli_rc = parsed_args.func(parsed_args)
    assert cli_rc == 0


def test_hermes_root_parser_routes_bare_requirement():
    """The real `hermes coop <requirement>` root-parser path reaches loop mode."""
    from plugins.agent_cooperation.cli import register_cli

    root = argparse.ArgumentParser(prog="hermes")
    root_sub = root.add_subparsers(dest="command")
    coop_parser = root_sub.add_parser("coop")
    register_cli(coop_parser)

    parsed = root.parse_args(["coop", "Add", "root", "parser", "feature", "--mock"])
    assert parsed.command == "coop"
    assert parsed.coop_action == "loop"
    assert parsed.requirement == ["Add", "root", "parser", "feature"]


def test_hermes_cli_near_miss_typo_does_not_launch_loop():
    """A typo like `stauts` must fail fast instead of spending budget on a loop."""
    from plugins.agent_cooperation.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes coop")
    register_cli(parser)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["stauts"])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_slash_command_near_miss_typo_is_rejected():
    from plugins.agent_cooperation.commands import handle_coop_command

    output = await handle_coop_command("stauts")
    assert "Unknown subcommand" in output
    assert "Did you mean `status`?" in output


def test_cli_loop_accepts_legacy_string_requirement(monkeypatch):
    from plugins.agent_cooperation import cli as cli_mod

    fake_bridge = SimpleNamespace(
        run_gemini_closed_loop=AsyncMock(
            return_value=SimpleNamespace(
                task_id="task_legacy",
                status="SUCCESS",
                state="SUCCESS",
                primary_model="gemini-3.8-flash-high",
                total_cost_cny=0.0,
                result_summary="",
                error="",
            )
        )
    )
    monkeypatch.setattr(cli_mod, "get_bridge", lambda: fake_bridge)
    args = argparse.Namespace(
        coop_action="loop",
        requirement="fix legacy single string",
        repo=None,
        title=None,
        reviewer="glm",
        mock=True,
        test_paths=None,
        max_repair=3,
    )

    assert cli_mod.coop_cli_command(args) == 0
    assert (
        fake_bridge.run_gemini_closed_loop.await_args.kwargs["requirement"]
        == "fix legacy single string"
    )


def test_cli_strips_wrapping_quotes_from_requirement():
    from plugins.agent_cooperation.cli import _requirement_text

    assert _requirement_text('"修复 登录 页面"') == "修复 登录 页面"
    assert _requirement_text(['"修复 登录"', '"页面"']) == "修复 登录 页面"


def test_recursive_test_discovery(mock_git_repo):
    """Verify _test_paths discovers tests inside nested subdirectories."""
    nested = mock_git_repo / "tests" / "unit"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "test_nested.py").write_text("def test_nested(): pass\n", encoding="utf-8")

    paths = GeminiClosedLoopOrchestrator._test_paths(mock_git_repo, [])
    assert "tests/unit/test_nested.py" in paths


class _CapturingDevAgent:
    """Minimal developer agent that records its context and returns a fixed result."""

    model_name = "gemini-3.8-flash-high"

    def __init__(self, result: AgentResult):
        self.result = result
        self.contexts: list[dict] = []

    async def execute(self, prompt, context=None):
        self.contexts.append(dict(context or {}))
        return self.result


@pytest.mark.asyncio
async def test_closed_loop_passes_repo_root_and_emits_policy_rejection_events(
    mock_git_repo, test_db_session, tmp_path,
):
    """Command-policy denials must reach the run stream as non-terminal tool events."""
    store = EventStore(tmp_path / "events")
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        event_store=store,
    )
    dev_agent = _CapturingDevAgent(AgentResult(
        success=False,
        content="",
        error="project command rejected",
        metadata={
            "policy_rejections": [
                {
                    "name": "run_command",
                    "command": "npm run build && whoami",
                    "reason": "forbidden shell fragment",
                },
                {
                    "name": "run_command",
                    "command": "python -m pytest -c evil.ini",
                    "reason": "workspace override option",
                },
            ],
        },
    ))

    result = await orchestrator.run(
        requirement="Trigger a policy rejection",
        mock_mode=True,
        dev_agent=dev_agent,
        task_id="task_policy_rejections",
    )

    assert result.success is False
    assert dev_agent.contexts
    assert dev_agent.contexts[0]["repo_root"] == str(mock_git_repo.resolve())

    events = store.read("task_policy_rejections")
    rejections = [
        event for event in events
        if event.type == "tool_result" and event.payload.get("policy_rejected") is True
    ]
    assert len(rejections) == 2
    assert rejections[0].agent == "gemini"
    assert rejections[0].phase == "develop"
    assert rejections[0].payload["name"] == "run_command"
    assert rejections[0].payload["succeeded"] is False
    assert rejections[0].payload["command"] == "npm run build && whoami"
    assert rejections[0].payload["reason"] == "forbidden shell fragment"
    assert rejections[1].payload["command"] == "python -m pytest -c evil.ini"
    assert events[-1].type == "result"
    assert events[-1].payload["terminal"] is True


def test_gemini_dev_agent_resolves_repo_scoped_and_explicit_command_policy(monkeypatch):
    """The declared policy must follow the task repository, not the process cwd."""
    execution = ExecutionConfig(allowed_commands={
        "default": {"test": [["python", "-m", "pytest"]]},
        "workplace/site": {
            "test": [["npm", "test"]],
            "build": [["npm", "run", "build"]],
        },
    })
    monkeypatch.setattr(
        type(settings),
        "load_policy",
        lambda self: SimpleNamespace(execution=execution),
    )
    agent = GeminiDevAgent(mock_mode=True)

    resolved = agent._resolve_allowed_commands({
        "repo_root": str(PROJECT_ROOT / "workplace/site"),
    })
    assert resolved == {
        "test": [["npm", "test"]],
        "build": [["npm", "run", "build"]],
        "typecheck": [],
    }
    assert agent._resolve_allowed_commands({"repo_root": None}) == {
        "test": [["python", "-m", "pytest"]],
        "build": [],
        "typecheck": [],
    }
    assert agent._resolve_allowed_commands({
        "allowed_commands": {"build": [["make", "build"]]},
    }) == {
        "test": [],
        "build": [["make", "build"]],
        "typecheck": [],
    }
    with pytest.raises(ValueError):
        agent._resolve_allowed_commands({"allowed_commands": {"test": [["pwsh", "-c", "whoami"]]}})


@pytest.mark.asyncio
async def test_closed_loop_emits_ordered_replayable_events(mock_git_repo, test_db_session, tmp_path):
    """Verify the closed loop publishes ordered status/output/test/usage/result events."""
    store = EventStore(tmp_path / "events")
    orchestrator = GeminiClosedLoopOrchestrator(
        repo_root=mock_git_repo,
        session=test_db_session,
        reviewer_choice="glm",
        max_repair_attempts=1,
        event_store=store,
    )

    dev_agent = GeminiDevAgent(mock_mode=True)
    reviewer_agent = FlashReviewer(model_key="glm", mock_mode=True)

    result = await orchestrator.run(
        requirement="Implement event instrumentation",
        mock_mode=True,
        dev_agent=dev_agent,
        reviewer_agent=reviewer_agent,
        task_id="task_events_test",
    )

    assert result.state == "SUCCESS"
    events = store.read("task_events_test")
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type == "result"
    assert events[-1].payload["state"] == "SUCCESS"
    assert events[-1].payload["success"] is True

    types = [event.type for event in events]
    for expected in ("status", "output", "diff", "usage", "test_line", "result"):
        assert expected in types, f"missing {expected} event"

    states = [e.payload.get("state") for e in events if e.type == "status" and e.phase == "state"]
    assert states[0] == "INIT"
    assert "DEVELOPING" in states
    assert "TESTING" in states
    assert "REVIEWING" in states
    assert states[-1] == "SUCCESS"

    dev_output = [e for e in events if e.type == "output" and e.agent == "gemini"]
    assert dev_output
    assert "Simulated implementation" in dev_output[0].payload.get("text", "")

    test_lines = [e.payload.get("line", "") for e in events if e.type == "test_line"]
    assert test_lines
    assert any("test session starts" in line or "passed" in line for line in test_lines)
    assert all(line != "stdout" for line in test_lines)

    assert events[-1].payload["prompt_tokens"] >= 0
    assert events[-1].payload["duration_s"] > 0


