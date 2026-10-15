"""Offline contract and behavioral tests for Gemini Dev + GLM/DeepSeek Review closed-loop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from agents.base import AgentResult
from agents.flash_reviewer import FlashReviewer
from agents.gemini_dev_agent import GeminiDevAgent
import sys
from config.settings import PolicyConfig, settings
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
async def test_closed_loop_p0_persistence_and_approval_resume(mock_git_repo, test_db_session):
    """Test full cycle: P0 halts in WAITING_APPROVAL -> resume with human approval -> Gemini repairs and succeeds."""
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

    bridge = get_bridge()
    bridge.session = test_db_session
    bridge.policy.security.allowed_repos = [str(mock_git_repo)]

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
    cli_rc = parsed_args.func(parsed_args)
    assert cli_rc == 0


def test_recursive_test_discovery(mock_git_repo):
    """Verify _test_paths discovers tests inside nested subdirectories."""
    nested = mock_git_repo / "tests" / "unit"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "test_nested.py").write_text("def test_nested(): pass\n", encoding="utf-8")

    paths = GeminiClosedLoopOrchestrator._test_paths(mock_git_repo, [])
    assert "tests/unit/test_nested.py" in paths


