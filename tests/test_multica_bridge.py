"""Tests for Multica platform integration, bridge, and worker."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.multica.bridge import MulticaBridge
from integrations.multica.client import (
    CliMulticaClient,
    HttpMulticaClient,
    MemoryMulticaClient,
)
from integrations.multica.models import (
    MulticaComment,
    MulticaIssue,
    MulticaRunResult,
    map_fsm_state_to_multica_status,
)
from integrations.multica.worker import MulticaWorker
from orchestrator.cli import execute_multica_issue, show_multica_board
from orchestrator.core import OrchestratorCore
from storage.models import Task


def test_multica_models_serialization(tmp_path: Path):
    issue = MulticaIssue(
        id="ISS-101",
        title="Cache Layer",
        description="Implement LRU Cache",
        status="todo",
        priority="high",
        labels=["backend", "cache"],
    )
    data = issue.to_dict()
    assert data["id"] == "ISS-101"
    assert data["priority"] == "high"

    restored = MulticaIssue.from_dict(data)
    assert restored.id == issue.id
    assert restored.title == issue.title
    assert restored.labels == ["backend", "cache"]

    comment = MulticaComment(issue_id="ISS-101", body="Work in progress", author="Worker1")
    cmt_data = comment.to_dict()
    assert cmt_data["author"] == "Worker1"
    assert MulticaComment.from_dict(cmt_data).body == "Work in progress"


def test_fsm_to_multica_status_mapping():
    assert map_fsm_state_to_multica_status("SUCCESS") == "done"
    assert map_fsm_state_to_multica_status("REVIEWING") == "in_review"
    assert map_fsm_state_to_multica_status("WAITING_REVIEW") == "in_review"
    assert map_fsm_state_to_multica_status("WAITING_APPROVAL") == "blocked"
    assert map_fsm_state_to_multica_status("BUDGET_STOPPED") == "blocked"
    assert map_fsm_state_to_multica_status("CODEX_BACKLOGGED") == "blocked"
    assert map_fsm_state_to_multica_status("FAILED") == "blocked"
    assert map_fsm_state_to_multica_status("INTERRUPTED") == "blocked"
    assert map_fsm_state_to_multica_status("DEVELOPING") == "in_progress"
    assert map_fsm_state_to_multica_status("TESTING") == "in_progress"
    assert map_fsm_state_to_multica_status("PLANNING") == "in_progress"


@pytest.mark.asyncio
async def test_memory_client_crud_and_persistence(tmp_path: Path):
    board_file = tmp_path / "board.json"
    client = MemoryMulticaClient(persistence_file=board_file)

    issue = MulticaIssue(id="ISS-201", title="Database Index", description="Add index to users table")
    await client.create_issue(issue)

    fetched = await client.get_issue("ISS-201")
    assert fetched is not None
    assert fetched.title == "Database Index"
    assert fetched.status == "todo"

    # Claim
    claimed = await client.claim_issue("ISS-201", assignee="AgentWorker")
    assert claimed.assignee == "AgentWorker"
    assert claimed.status == "in_progress"

    # Comments
    await client.post_comment("ISS-201", "Starting task", author="AgentWorker")
    comments = await client.list_comments("ISS-201")
    assert len(comments) == 1
    assert comments[0].body == "Starting task"

    # Update status
    await client.update_issue_status("ISS-201", status="done", branch_name="feat/db-index")
    updated = await client.get_issue("ISS-201")
    assert updated.status == "done"
    assert updated.branch_name == "feat/db-index"

    # Reload from file into new client
    client2 = MemoryMulticaClient(persistence_file=board_file)
    restored_issue = await client2.get_issue("ISS-201")
    assert restored_issue is not None
    assert restored_issue.status == "done"
    assert restored_issue.branch_name == "feat/db-index"
    assert len(await client2.list_comments("ISS-201")) == 1


def test_cli_and_http_clients():
    with patch("shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError):
            CliMulticaClient("nonexistent_multica_cli")

    http_client = HttpMulticaClient("http://my-multica-server:8080", token="secret-token-123")
    assert http_client.base_url == "http://my-multica-server:8080"
    assert http_client.token == "secret-token-123"


@pytest.mark.asyncio
async def test_multica_bridge_run_success():
    client = MemoryMulticaClient()
    issue = MulticaIssue(
        id="ISS-301",
        title="Cache Implementation",
        description="Implement an in-memory cache with eviction policy",
    )
    await client.create_issue(issue)

    mock_core = MagicMock()
    mock_core.policy.primary_agent = "glm"
    mock_task = Task(
        task_id="task_test_123",
        title="Cache Implementation",
        requirement="Implement an in-memory cache",
        state="SUCCESS",
        branch_name="codex/task_test_123",
        worktree_path="/tmp/worktree",
        repair_attempts=1,
        total_cost_cny=0.015,
        result_summary="All 10 unit tests passed successfully.",
    )
    mock_core.run_task = AsyncMock(return_value=mock_task)

    bridge = MulticaBridge(client=client, core=mock_core, assignee_name="AgentCoop-Worker")
    result = await bridge.run_issue(issue, chosen_model="glm", mock_mode=True)

    assert result.issue_id == "ISS-301"
    assert result.multica_status == "done"
    assert result.fsm_state == "SUCCESS"
    assert result.branch_name == "codex/task_test_123"
    assert len(result.comments_posted) == 2

    # Verify state in client
    updated_issue = await client.get_issue("ISS-301")
    assert updated_issue.status == "done"
    assert updated_issue.assignee == "AgentCoop-Worker"
    assert updated_issue.branch_name == "codex/task_test_123"

    comments = await client.list_comments("ISS-301")
    assert len(comments) == 2
    assert "任务已认领" in comments[0].body
    assert "执行报告" in comments[1].body
    assert "All 10 unit tests passed successfully." in comments[1].body


@pytest.mark.asyncio
async def test_multica_bridge_run_blocked():
    client = MemoryMulticaClient()
    issue = MulticaIssue(
        id="ISS-302",
        title="High Risk Module",
        description="Refactor critical payment logic",
    )
    await client.create_issue(issue)

    mock_core = MagicMock()
    mock_core.policy.primary_agent = "deepseek"
    mock_task = Task(
        task_id="task_test_blocked",
        title="High Risk Module",
        requirement="Refactor critical payment logic",
        state="WAITING_APPROVAL",
        branch_name="codex/task_blocked",
        worktree_path="/tmp/worktree_blocked",
        repair_attempts=2,
        total_cost_cny=0.02,
        result_summary="P0 architecture conflict encountered.",
    )
    mock_core.run_task = AsyncMock(return_value=mock_task)

    bridge = MulticaBridge(client=client, core=mock_core)
    result = await bridge.run_issue(issue, chosen_model="deepseek", mock_mode=True)

    assert result.multica_status == "blocked"
    assert result.fsm_state == "WAITING_APPROVAL"

    updated_issue = await client.get_issue("ISS-302")
    assert updated_issue.status == "blocked"

    comments = await client.list_comments("ISS-302")
    assert len(comments) == 2
    assert "人工审批提醒" in comments[1].body


@pytest.mark.asyncio
async def test_multica_worker_poll_once():
    client = MemoryMulticaClient()
    await client.create_issue(MulticaIssue(id="ISS-401", title="Task 1", description="Implement feature 1"))
    await client.create_issue(MulticaIssue(id="ISS-402", title="Task 2", description="Implement feature 2"))

    mock_core = MagicMock()
    mock_core.policy.primary_agent = "glm"
    mock_task = Task(
        task_id="task_w_1",
        title="Worker Task",
        requirement="Req",
        state="SUCCESS",
        branch_name="codex/task_w_1",
        total_cost_cny=0.005,
        result_summary="Tests passed",
    )
    mock_core.run_task = AsyncMock(return_value=mock_task)

    bridge = MulticaBridge(client=client, core=mock_core)
    worker = MulticaWorker(bridge=bridge, poll_interval=1, mock_mode=True)

    results = await worker.poll_once()
    assert len(results) == 2
    assert all(r.multica_status == "done" for r in results)

    # Subsequent poll should find nothing in 'todo'
    results_second = await worker.poll_once()
    assert len(results_second) == 0


@pytest.mark.asyncio
async def test_cli_execute_multica_issue(tmp_path: Path):
    board_file = tmp_path / "test_board.json"

    with patch("orchestrator.cli._core_scope") as mock_scope:
        mock_core = MagicMock()
        mock_core.policy.primary_agent = "glm"
        mock_task = Task(
            task_id="task_cli_test",
            title="CLI Issue",
            requirement="Req CLI",
            state="SUCCESS",
            branch_name="codex/task_cli_test",
            total_cost_cny=0.001,
            result_summary="Passed CLI integration",
        )
        mock_core.run_task = AsyncMock(return_value=mock_task)
        mock_scope.return_value.__enter__.return_value = mock_core

        res = await execute_multica_issue(
            issue_id="ISS-CLI-1",
            title="CLI Test Issue",
            task_req="Testing CLI bridge execution",
            model="glm",
            mock=True,
            persistence_file=board_file,
        )
        assert res.issue_id == "ISS-CLI-1"
        assert res.multica_status == "done"
        assert board_file.exists()

        # Check show_multica_board does not crash
        show_multica_board(persistence_file=board_file)
