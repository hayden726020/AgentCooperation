"""Comprehensive test suite for Hermes Agent integration with AgentCooperation.

Covers:
1. Hermes plugin discovery and registration (tools, slash commands, prompt sections)
2. Persistent request deduplication (idempotency key and auto-hash)
3. Hard budget interception and rejection
4. Authorized repository security validation
5. Execution failure and propagation
6. Review rejection and P0 human approval lifecycle
7. State recovery and query inspection
8. Direct role bot execution (Dev Plan, Reviewer, Codex Expert)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from config.settings import BudgetConfig, PolicyConfig, SecurityConfig
from integrations.hermes.bridge import HermesCooperationBridge
from integrations.hermes.models import (
    HermesApprovalRequest,
    HermesSubmitRequest,
    HermesTaskResponse,
)
from storage.models import (
    BudgetReservation,
    ExecutionLog,
    IdempotencyRecord,
    MonthlySpending,
    PendingCodexAudit,
    Task,
)


@pytest.fixture
def isolated_git_repo(tmp_path: Path):
    """Create a temporary initialized Git repository with an initial commit."""
    repo = tmp_path / "target_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# Target Repository\n", encoding="utf-8")
    for args in (
        ["init"],
        ["config", "user.name", "HermesTester"],
        ["config", "user.email", "tester@example.com"],
        ["add", "README.md"],
        ["commit", "-m", "Initial commit"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def hermes_test_env(tmp_path: Path, isolated_git_repo: Path):
    """Isolated database and bridge environment for testing."""
    db_file = tmp_path / "hermes_test.db"
    test_engine = create_engine(
        f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(test_engine)
    session = Session(test_engine)

    policy = PolicyConfig(
        primary_agent="glm",
        reviewer_agent="antigravity_cli",
        expert_agent="codex_cli",
        budget=BudgetConfig(
            monthly_cny=200.0,
            warning_cny=160.0,
            pause_new_cny=190.0,
            hard_stop_cny=200.0,
            task_limit_cny=10.0,
        ),
        security=SecurityConfig(allowed_repos=[str(isolated_git_repo)]),
    )

    bridge = HermesCooperationBridge(
        repo_root=isolated_git_repo, session=session, policy=policy
    )
    yield bridge, session, isolated_git_repo, policy
    session.close()
    test_engine.dispose()


# =============================================================================
# 1. Hermes Plugin Discovery and Registration
# =============================================================================
def test_hermes_plugin_discovery_and_registration():
    """Verify that Hermes PluginManager discovers agent_cooperation plugin and registers all components."""
    import sys
    hermes_dir = Path(__file__).resolve().parent.parent / "hermes-agent"
    if str(hermes_dir) not in sys.path:
        sys.path.insert(0, str(hermes_dir))

    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    from toolsets import resolve_toolset

    pm = PluginManager()
    pm.discover_and_load()

    # 1. Plugin loaded and enabled
    plugin = pm._plugins.get("agent_cooperation")
    assert plugin is not None, "agent_cooperation plugin must be discovered"
    assert plugin.enabled is True, "agent_cooperation plugin must be enabled"

    # 2. Registered tools in registry
    registered_tools = registry.get_all_tool_names()
    expected_tools = [
        "coop_submit_task",
        "coop_query_task",
        "coop_list_tasks",
        "coop_get_pending_approvals",
        "coop_approve_task",
        "coop_check_budget",
        "coop_run_dev_plan",
        "coop_run_review",
        "coop_run_expert_diagnose",
    ]
    for tool_name in expected_tools:
        assert tool_name in registered_tools, f"Tool {tool_name} must be in registry"

    # 3. Toolset resolution
    coop_toolset = resolve_toolset("agent_cooperation")
    for tool_name in expected_tools:
        assert tool_name in coop_toolset, f"Tool {tool_name} must be in agent_cooperation toolset"

    # 4. In-session slash command
    assert "coop" in pm._plugin_commands, "Slash command /coop must be registered"

    # 5. System prompt section
    assert "agent_cooperation_matrix" in pm._system_prompt_sections, "Prompt section must be registered"


# =============================================================================
# 2. Persistent Request Deduplication (Idempotency)
# =============================================================================
@pytest.mark.asyncio
async def test_idempotent_task_deduplication(hermes_test_env):
    """Submitting duplicate task with same idempotency key returns cached result without re-executing."""
    bridge, session, repo, policy = hermes_test_env

    req = HermesSubmitRequest(
        requirement="Implement a stack data structure in Python",
        title="Stack Data Structure",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
        idempotency_key="idempotency-key-stack-001",
    )

    # First run
    resp1 = await bridge.submit_task(req)
    assert resp1.status == "SUCCESS"
    assert resp1.deduplicated is False
    assert resp1.task_id != ""

    # Second run with exact same idempotency_key
    resp2 = await bridge.submit_task(req)
    assert resp2.deduplicated is True
    assert resp2.task_id == resp1.task_id
    assert resp2.status == resp1.status
    assert resp2.state == resp1.state

    # Verify only ONE task exists in database
    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 1
    assert tasks[0].task_id == resp1.task_id

    # Verify IdempotencyRecord
    record = session.exec(
        select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == "key_idempotency-key-stack-001"
        )
    ).first()
    assert record is not None
    assert record.status == "COMPLETED"
    assert record.task_id == resp1.task_id


@pytest.mark.asyncio
async def test_auto_hash_deduplication(hermes_test_env):
    """When no explicit idempotency key is provided, deterministic hash of (repo, model, requirement) deduplicates."""
    bridge, session, repo, policy = hermes_test_env

    req = HermesSubmitRequest(
        requirement="Implement queue data structure",
        title="Queue Data Structure",
        repo_root=str(repo),
        chosen_model="deepseek",
        mock_mode=True,
    )

    resp1 = await bridge.submit_task(req)
    assert resp1.status == "SUCCESS"
    assert resp1.deduplicated is False

    resp2 = await bridge.submit_task(req)
    assert resp2.deduplicated is True
    assert resp2.task_id == resp1.task_id


# =============================================================================
# 3. Hard Budget Interception
# =============================================================================
@pytest.mark.asyncio
async def test_hard_budget_rejection(hermes_test_env):
    """When monthly spending reaches pause_new_cny / hard_stop_cny, submission is rejected immediately."""
    bridge, session, repo, policy = hermes_test_env

    # Simulate existing month spending at ¥195 (exceeding pause_new_cny = ¥190)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    session.add(
        MonthlySpending(
            year_month=month,
            total_cost_cny=195.0,
            total_prompt_tokens=10000,
            total_completion_tokens=5000,
        )
    )
    session.commit()

    req = HermesSubmitRequest(
        requirement="Develop caching subsystem",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=False,  # Live mode triggers real budget check
        idempotency_key="budget-test-key",
    )

    resp = await bridge.submit_task(req)
    assert resp.status == "REJECTED"
    assert resp.state == "BUDGET_STOPPED"
    assert "Pause New Tasks" in (resp.error or "")

    # Ensure no task was created and no budget was reserved
    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 0

    reservations = session.exec(select(BudgetReservation)).all()
    assert len(reservations) == 0

    # Ensure rejection is recorded in idempotency store
    record = session.exec(
        select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "key_budget-test-key")
    ).first()
    assert record is not None
    assert record.status == "REJECTED"

    # Retry should also return rejected without executing
    retry_resp = await bridge.submit_task(req)
    assert retry_resp.status == "REJECTED"
    assert retry_resp.deduplicated is True


# =============================================================================
# 4. Security & Authorized Repositories
# =============================================================================
def test_unauthorized_repository_rejected(hermes_test_env, tmp_path: Path):
    """Directories outside policy.security.allowed_repos are rejected immediately."""
    bridge, session, repo, policy = hermes_test_env

    unauthorized_dir = tmp_path / "unauthorized_repo"
    unauthorized_dir.mkdir()

    with pytest.raises(PermissionError) as exc_info:
        bridge.validate_repo_root(unauthorized_dir)
    assert "Unauthorized repository path" in str(exc_info.value)


def test_non_git_directory_rejected(hermes_test_env, tmp_path: Path):
    """An authorized directory without .git is rejected."""
    bridge, session, repo, policy = hermes_test_env

    non_git = repo / "subdir"
    non_git.mkdir()
    policy.security.allowed_repos.append(str(non_git))

    with pytest.raises(ValueError) as exc_info:
        bridge.validate_repo_root(non_git)
    assert "Directory is not a Git repository" in str(exc_info.value)


# =============================================================================
# 5. Execution Failure Propagation
# =============================================================================
@pytest.mark.asyncio
async def test_execution_failure_propagation(hermes_test_env):
    """When the underlying primary agent fails fatally, state records failure and propagates."""
    bridge, session, repo, policy = hermes_test_env

    with patch.object(bridge.core, "run_task", side_effect=RuntimeError("Primary model failed with code 500")):
        req = HermesSubmitRequest(
            requirement="Implement feature that errors out",
            repo_root=str(repo),
            mock_mode=True,
            idempotency_key="failure-prop-001",
        )
        resp = await bridge.submit_task(req)

        assert resp.status == "FAILED"
        assert resp.state == "FAILED"
        assert "Primary model failed with code 500" in (resp.error or "")

        # Verify idempotency record reflects failure
        record = session.exec(
            select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "key_failure-prop-001")
        ).first()
        assert record is not None
        assert record.status == "FAILED"


# =============================================================================
# 6. Review Rejection and P0 Human Approval Lifecycle
# =============================================================================
@pytest.mark.asyncio
async def test_review_p0_and_approval_lifecycle(hermes_test_env):
    """P0 review rejection halts in WAITING_APPROVAL, listed in pending approvals, and resolves on human decision through verification."""
    bridge, session, repo, policy = hermes_test_env

    # 1. Create a task in WAITING_APPROVAL state with real worktree
    task_id = "task_p0_escalation_123"
    worktree = (await bridge.core.worktree_mgr.create_worktree(task_id)).resolve()
    base_ref = await bridge.core.worktree_mgr.get_base_ref(worktree)
    (worktree / "solution.py").write_text("def solve(): return True\n", encoding="utf-8")
    (worktree / "test_solution.py").write_text("from solution import solve\ndef test_solution(): assert solve() is True\n", encoding="utf-8")
    await bridge.core.worktree_mgr.commit_checkpoint(worktree, "feat: initial solution")

    p0_task = Task(
        task_id=task_id,
        title="Security Critical Feature",
        requirement="Implement auth without credentials exposure",
        primary_model="glm",
        state="WAITING_APPROVAL",
        worktree_path=str(worktree),
        result_summary="P0 risk requires authorized human approval; no automatic expert patch applied",
    )
    session.add(p0_task)
    session.add(
        ExecutionLog(
            task_id=task_id,
            step_name="Reviewer Evaluation",
            agent_role="REVIEWER",
            agent_model="antigravity_cli",
            status="SUCCESS",
            output_snippet=json.dumps({"approved": False, "severity": "P0", "summary": "Critical vulnerability detected"}),
        )
    )
    session.commit()

    # 2. Query pending approvals
    pending = bridge.get_pending_approvals()
    p0_list = pending.get("p0_waiting_approval", [])
    assert len(p0_list) == 1
    assert p0_list[0]["task_id"] == task_id

    # 3. Approve task with mock_mode=True
    app_req = HermesApprovalRequest(
        task_id=task_id,
        approved=True,
        notes="Security fix reviewed and manual exception granted.",
        mock_mode=True,
    )
    app_resp = await bridge.approve_task(app_req)
    assert app_resp.success is True
    assert app_resp.updated_state == "SUCCESS"

    # 4. Verify task state in database
    refreshed_task = session.exec(select(Task).where(Task.task_id == task_id)).first()
    assert refreshed_task.state == "SUCCESS"
    assert "passed after human approval" in (refreshed_task.result_summary or "")
    assert refreshed_task.completed_at is not None

    # 5. Pending approvals list is now empty
    pending_after = bridge.get_pending_approvals()
    assert len(pending_after.get("p0_waiting_approval", [])) == 0


@pytest.mark.asyncio
async def test_review_rejection_rejection_decision(hermes_test_env):
    """Human rejecting a WAITING_APPROVAL task transitions it to FAILED."""
    bridge, session, repo, policy = hermes_test_env

    task = Task(
        task_id="task_p0_rejected_456",
        title="Unsafe Patch",
        requirement="Unsafe operation",
        primary_model="deepseek",
        state="WAITING_APPROVAL",
    )
    session.add(task)
    session.commit()

    app_req = HermesApprovalRequest(
        task_id="task_p0_rejected_456",
        approved=False,
        notes="Unacceptable security risk.",
    )
    app_resp = await bridge.approve_task(app_req)
    assert app_resp.success is True
    assert app_resp.updated_state == "FAILED"

    refreshed = session.exec(select(Task).where(Task.task_id == "task_p0_rejected_456")).first()
    assert refreshed.state == "FAILED"
    assert "Rejected by human operator" in (refreshed.result_summary or "")


# =============================================================================
# 7. State Recovery and Query Inspection
# =============================================================================
def test_query_task_and_list_tasks(hermes_test_env):
    """Querying and listing tasks recovers full state and execution metadata."""
    bridge, session, repo, policy = hermes_test_env

    t1 = Task(task_id="task_1", title="Task 1", requirement="Req 1", state="SUCCESS", primary_model="glm", total_cost_cny=0.05)
    t2 = Task(task_id="task_2", title="Task 2", requirement="Req 2", state="FAILED", primary_model="deepseek", total_cost_cny=0.08)
    session.add_all([t1, t2])
    session.add(ExecutionLog(task_id="task_1", step_name="Testing", agent_role="TEST_RUNNER", agent_model="LocalRunner", status="SUCCESS", output_snippet="5 passed"))
    session.commit()

    # Query single task
    q_resp = bridge.query_task("task_1")
    assert q_resp is not None
    assert q_resp.task_id == "task_1"
    assert q_resp.status == "SUCCESS"
    assert q_resp.test_summary == "5 passed"

    # Query nonexistent
    assert bridge.query_task("task_nonexistent") is None

    # List tasks
    all_tasks = bridge.list_tasks()
    assert len(all_tasks) == 2

    # Filtered list
    success_only = bridge.list_tasks(status_filter="SUCCESS")
    assert len(success_only) == 1
    assert success_only[0].task_id == "task_1"


# =============================================================================
# 8. Direct Role Bot Execution
# =============================================================================
@pytest.mark.asyncio
async def test_direct_role_dev_plan(hermes_test_env):
    """Directly invoking Primary Dev Agent produces plan output with budget check."""
    bridge, session, repo, policy = hermes_test_env

    resp = await bridge.run_role_dev_plan(
        requirement="Plan a rate limiter component",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
    )

    assert resp.success is True
    assert "Primary Dev" in resp.role
    assert resp.content != ""
    assert resp.prompt_tokens > 0


@pytest.mark.asyncio
async def test_direct_role_reviewer(hermes_test_env):
    """Directly invoking Independent Reviewer evaluates evidence and returns verdict."""
    bridge, session, repo, policy = hermes_test_env

    resp = await bridge.run_role_reviewer(
        worktree_path=str(repo),
        requirement="Implement basic logger",
        git_diff="+ def log(msg): print(msg)",
        test_summary="test_logger.py . [100%]",
        mock_mode=True,
    )

    assert resp.success is True
    assert "Reviewer" in resp.role
    assert resp.metadata.get("review") is not None
    review = resp.metadata["review"]
    assert "approved" in review
    assert "severity" in review


@pytest.mark.asyncio
async def test_direct_role_expert(hermes_test_env):
    """Directly invoking Codex Expert provides diagnosis advice."""
    bridge, session, repo, policy = hermes_test_env

    resp = await bridge.run_role_expert(
        issue_context="Tests failed: RecursionError on tree traversal",
        worktree_path=str(repo),
        mock_mode=True,
    )

    assert resp.success is True
    assert resp.role == "Codex Expert"
    assert resp.content != ""


# =============================================================================
# 9. Hermes CLI and Slash Command Tests
# =============================================================================
@pytest.mark.asyncio
async def test_coop_slash_commands(hermes_test_env):
    """Test /coop slash commands handler."""
    bridge, session, repo, policy = hermes_test_env
    from plugins.agent_cooperation.commands import handle_coop_command
    import plugins.agent_cooperation.tools as tools_mod
    tools_mod._bridge_instance = bridge

    # 1. Help
    help_out = await handle_coop_command("help")
    assert "/coop run" in help_out
    assert "/coop budget" in help_out

    # 2. Budget
    budget_out = await handle_coop_command("budget")
    assert "Monthly Spending" in budget_out
    assert "Codex Quota State" in budget_out

    # 3. Pending
    pending_out = await handle_coop_command("pending")
    assert "Pending Actions" in pending_out

    # 4. Unknown subcommand
    unknown_out = await handle_coop_command("nonexistent_subcmd")
    assert "Unknown subcommand" in unknown_out


def test_coop_cli_commands(hermes_test_env, capsys):
    """Test hermes coop CLI parser and execution handlers."""
    bridge, session, repo, policy = hermes_test_env
    from plugins.agent_cooperation.cli import coop_cli_command
    import plugins.agent_cooperation.tools as tools_mod
    import argparse
    tools_mod._bridge_instance = bridge

    # 1. Budget action
    args_budget = argparse.Namespace(coop_action="budget")
    rc = coop_cli_command(args_budget)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Monthly Spending" in captured.out

    # 2. Pending action
    args_pending = argparse.Namespace(coop_action="pending")
    rc = coop_cli_command(args_pending)
    assert rc == 0
    captured = capsys.readouterr()
    assert "P0 Tasks Waiting Approval" in captured.out

    # 3. List action
    args_list = argparse.Namespace(coop_action="list", limit=5, filter=None)
    rc = coop_cli_command(args_list)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Found" in captured.out

    # 4. No action provided
    args_empty = argparse.Namespace()
    rc = coop_cli_command(args_empty)
    assert rc == 2


@pytest.mark.asyncio
async def test_mcp_server_tool_calls(hermes_test_env):
    """Test MCP tool call handlers."""
    bridge, session, repo, policy = hermes_test_env
    import integrations.hermes.mcp_server as mcp_mod
    mcp_mod.BRIDGE = bridge

    # 1. Check budget tool call
    res_budget = await mcp_mod.handle_tool_call("coop_check_budget", {})
    assert "monthly_cny" in res_budget
    assert "can_start_new_task" in res_budget

    # 2. Pending approvals tool call
    res_pending = await mcp_mod.handle_tool_call("coop_get_pending_approvals", {})
    assert "p0_waiting_approval" in res_pending
    assert "queued_codex_audits" in res_pending

    # 3. List tasks tool call
    res_list = await mcp_mod.handle_tool_call("coop_list_tasks", {"limit": 5})
    assert isinstance(res_list, list)

    # 4. Unknown tool call
    with pytest.raises(ValueError) as exc:
        await mcp_mod.handle_tool_call("unknown_tool", {})
    assert "Unknown tool" in str(exc.value)


# =============================================================================
# 10. Additional Behavioral Tests: Model configs, Idempotency isolation, Quota & Boundaries
# =============================================================================
@pytest.mark.asyncio
async def test_mock_and_live_idempotency_isolation(hermes_test_env):
    """Auto idempotency keys isolate mock requests from live requests; live does not return cached mock SUCCESS."""
    bridge, session, repo, policy = hermes_test_env

    mock_req = HermesSubmitRequest(
        requirement="Implement trie data structure",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
    )
    live_req = HermesSubmitRequest(
        requirement="Implement trie data structure",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=False,
    )

    key_mock = bridge.compute_idempotency_key(repo, mock_req.requirement, "glm", mock_mode=True)
    key_live = bridge.compute_idempotency_key(repo, live_req.requirement, "glm", mock_mode=False)
    assert key_mock != key_live
    assert key_mock.startswith("auto_")
    assert key_live.startswith("auto_")

    resp_mock = await bridge.submit_task(mock_req)
    assert resp_mock.status == "SUCCESS"
    assert resp_mock.deduplicated is False

    # Simulate live request under budget limit - must not reuse mock response
    with patch.object(bridge.core.budget_guard, "check_can_start_new_task", return_value=(False, "Pause New Tasks")):
        resp_live = await bridge.submit_task(live_req)
        assert resp_live.status == "REJECTED"
        assert resp_live.task_id != resp_mock.task_id


@pytest.mark.asyncio
async def test_custom_idempotency_key_mismatch_rejected(hermes_test_env):
    """Reusing custom idempotency key for conflicting parameters raises ValueError."""
    bridge, session, repo, policy = hermes_test_env

    req1 = HermesSubmitRequest(
        requirement="First requirement text",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
        idempotency_key="shared-custom-key-01",
    )
    resp1 = await bridge.submit_task(req1)
    assert resp1.status == "SUCCESS"

    req2 = HermesSubmitRequest(
        requirement="Different conflicting requirement text",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
        idempotency_key="shared-custom-key-01",
    )
    with pytest.raises(ValueError) as exc:
        await bridge.submit_task(req2)
    assert "incompatible request parameters" in str(exc.value)


@pytest.mark.asyncio
async def test_budget_recovery_allows_retry(hermes_test_env):
    """A task rejected due to budget limit can be retried once budget is restored without permanent cache block."""
    bridge, session, repo, policy = hermes_test_env

    # Simulate over budget
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    spending = MonthlySpending(
        year_month=month,
        total_cost_cny=195.0,
        total_prompt_tokens=10000,
        total_completion_tokens=5000,
    )
    session.add(spending)
    session.commit()

    req = HermesSubmitRequest(
        requirement="Feature blocked by budget",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=False,
        idempotency_key="recoverable-key-01",
    )
    resp1 = await bridge.submit_task(req)
    assert resp1.status == "REJECTED"

    # Now simulate budget recovery (e.g. spending reset)
    spending.total_cost_cny = 50.0
    session.add(spending)
    session.commit()

    # Retry should now succeed (with mocked run_task to avoid real cost)
    with patch.object(bridge.core, "run_task") as mock_run:
        mock_task = Task(
            task_id="task_recovered_1",
            title="Feature blocked by budget",
            requirement="Feature blocked by budget",
            state="SUCCESS",
            primary_model="glm",
        )
        mock_run.return_value = mock_task
        resp2 = await bridge.submit_task(req)
        assert resp2.status == "SUCCESS"
        assert resp2.task_id == "task_recovered_1"


@pytest.mark.asyncio
async def test_reviewer_binds_full_git_commit_sha(hermes_test_env):
    """run_role_reviewer resolves and passes full 40-character Git SHA to reviewer agent."""
    bridge, session, repo, policy = hermes_test_env

    captured_context = {}
    async def mock_execute(prompt, context=None):
        nonlocal captured_context
        captured_context = context or {}
        from agents.base import AgentResult
        return AgentResult(True, '{"approved": true, "severity": "P3", "summary": "ok", "critical_issues": [], "suggestions": []}',
                           metadata={"review": {"approved": True, "severity": "P3"}})

    with patch.object(bridge.core, "_get_reviewer") as mock_get_rev:
        mock_reviewer = MagicMock()
        mock_reviewer.execute = mock_execute
        mock_reviewer.model_name = "MockReviewer"
        mock_get_rev.return_value = mock_reviewer

        resp = await bridge.run_role_reviewer(
            worktree_path=str(repo),
            requirement="Audit code",
            git_diff="+ def foo(): pass",
            test_summary="1 passed",
            mock_mode=True,
        )
        assert resp.success is True
        sha = captured_context.get("expected_commit_sha")
        assert sha is not None
        assert len(sha) == 40
        assert sha != "HEAD"


@pytest.mark.asyncio
async def test_direct_roles_security_and_quota_enforcement(hermes_test_env, tmp_path):
    """Direct roles strictly enforce repo authorization, codex policy, and worktree isolation."""
    bridge, session, repo, policy = hermes_test_env

    # 1. Unauthorized repo for Reviewer
    unauthorized = tmp_path / "rogue_repo"
    unauthorized.mkdir()
    with pytest.raises(PermissionError):
        await bridge.run_role_reviewer(worktree_path=str(unauthorized), requirement="Review rogue repo")

    # 2. Unauthorized repo for Expert
    with pytest.raises(PermissionError):
        await bridge.run_role_expert(issue_context="Issue in rogue repo", worktree_path=str(unauthorized))

    # 3. Disabled Codex in policy
    policy.codex.enabled = False
    with pytest.raises(PermissionError) as exc:
        await bridge.run_role_expert(issue_context="Issue", worktree_path=str(repo))
    assert "disabled in policy" in str(exc.value)
    policy.codex.enabled = True

    # 4. Dev Plan isolation: does not dirty main repo with TASK.md or SPEC.json
    plan_resp = await bridge.run_role_dev_plan(
        requirement="Generate spec for parser",
        repo_root=str(repo),
        chosen_model="glm",
        mock_mode=True,
    )
    assert plan_resp.success is True
    # Verify main repo is clean
    assert not (repo / "TASK.md").exists()
    assert not (repo / "SPEC.json").exists()


@pytest.mark.asyncio
async def test_slash_command_error_handling(hermes_test_env):
    """Slash command safely catches unclosed quotes, invalid options, and parses --limit."""
    bridge, session, repo, policy = hermes_test_env
    from plugins.agent_cooperation.commands import handle_coop_command
    import plugins.agent_cooperation.tools as tools_mod
    tools_mod._bridge_instance = bridge

    # 1. Unclosed quotes in shlex
    err_quote = await handle_coop_command('run "unclosed quote string')
    assert "Command parsing error" in err_quote

    # 2. Argument error does not crash host
    err_arg = await handle_coop_command("run")
    assert "Command argument error" in err_arg or "❌" in err_arg

    # 3. list --limit parsing
    list_out = await handle_coop_command("list --limit 5")
    assert "Recent Tasks" in list_out or "No tasks found" in list_out
