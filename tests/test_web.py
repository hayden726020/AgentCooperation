"""Unit tests for the Web Console application and REST API endpoints."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from web.app import create_app
from web.runner import job_manager


@pytest.fixture
def app():
    app = create_app(mock_default=True)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "healthy"


@pytest.mark.parametrize("path", ["/api/tasks", "/api/multica/run", "/api/multica/issue", "/api/codex/quota", "/api/codex/recovery", "/api/codex/backlog/trigger", "/api/runs", "/api/runs/example/approve", "/api/runs/example/reject"])
def test_all_writes_reject_cross_origin(client, path):
    response = client.post(path, json={}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_dns_rebinding_host_rejected_even_with_matching_origin(client):
    response = client.post("/api/runs", json={}, headers={"Host": "evil.example", "Origin": "http://evil.example"})
    assert response.status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("address", ["192.0.2.1", "::ffff:192.0.2.1", ""])
def test_remote_client_cannot_write_with_loopback_host(client, address):
    response = client.post("/api/runs", json={}, headers={"Host": "localhost", "X-Forwarded-For": "127.0.0.1"}, environ_overrides={"REMOTE_ADDR": address})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/health", "/api/runs", "/api/status"])
def test_remote_client_cannot_read_with_loopback_host(client, path):
    response = client.get(path, headers={"Host": "localhost", "X-Forwarded-For": "127.0.0.1"}, environ_overrides={"REMOTE_ADDR": "192.0.2.1"})
    assert response.status_code == 403


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"AgentCooperation" in resp.data
    assert b"tab-dashboard" in resp.data


def test_api_status_mock(client):
    resp = client.get("/api/status?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mock_mode"] is True
    assert "spending" in data
    assert "codex_quota" in data
    assert "tasks_count" in data
    assert data["spending"]["monthly_limit_cny"] > 0


def test_api_status_live(client):
    resp = client.get("/api/status?mock=false")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mock_mode"] is False
    assert "spending" in data
    assert "codex_quota" in data


def test_api_list_tasks(client):
    resp = client.get("/api/tasks?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "tasks" in data
    assert isinstance(data["tasks"], list)


def test_api_task_detail_not_found(client):
    resp = client.get("/api/tasks/non_existent_task_id?mock=true")
    assert resp.status_code == 404
    data = resp.get_json()
    assert "error" in data


def test_api_task_detail_existing(client):
    # First get list of tasks to find one ID
    list_resp = client.get("/api/tasks?mock=false")
    tasks = list_resp.get_json().get("tasks", [])
    if tasks:
        task_id = tasks[0]["task_id"]
        detail_resp = client.get(f"/api/tasks/{task_id}?mock=false")
        assert detail_resp.status_code == 200
        detail_data = detail_resp.get_json()
        assert "task" in detail_data
        assert detail_data["task"]["task_id"] == task_id
        assert "logs" in detail_data


def test_api_create_task_validation(client):
    resp = client.post("/api/tasks", json={})
    assert resp.status_code == 400

    resp = client.post("/api/tasks", json={"requirement": "test", "model": "invalid_model"})
    assert resp.status_code == 400


def test_api_create_task_success(client):
    resp = client.post("/api/tasks", json={
        "requirement": "测试创建任务",
        "title": "测试任务",
        "model": "glm",
        "mock": True,
    })
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "job_id" in data

    # Check job status
    job_id = data["job_id"]
    job_resp = client.get(f"/api/jobs/{job_id}")
    assert job_resp.status_code == 200
    job_data = job_resp.get_json()
    assert job_data["id"] == job_id
    assert job_data["type"] == "run_task"


def test_api_create_task_runs_cli_quietly(client, monkeypatch):
    execute = AsyncMock(return_value=SimpleNamespace(
        task_id="task_quiet", state="SUCCESS", total_cost_cny=0.0,
        branch_name=None, result_summary="ok"))
    monkeypatch.setattr("web.api.execute_task", execute)

    resp = client.post("/api/tasks", json={
        "requirement": "quiet", "title": "quiet", "model": "glm", "mock": True,
    })
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.02)

    assert execute.await_count >= 1
    assert all(call.kwargs.get("quiet") is True for call in execute.await_args_list)


def test_api_multica_board(client):
    resp = client.get("/api/multica/board")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "columns" in data
    assert "todo" in data["columns"]
    assert "in_progress" in data["columns"]
    assert "done" in data["columns"]


def test_api_multica_create_issue(client, tmp_path):
    board_file = tmp_path / "test_board.json"
    resp = client.post("/api/multica/issue", json={
        "title": "测试看板任务",
        "description": "单元测试生成的 Issue",
        "priority": "high",
        "board_file": str(board_file),
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["success"] is True
    assert data["issue"]["title"] == "测试看板任务"


def test_api_codex_quota(client):
    resp = client.get("/api/codex/quota?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "quota" in data
    assert "state" in data["quota"]
    assert "audits" in data


def test_api_update_codex_quota(client):
    resp = client.post("/api/codex/quota", json={
        "state": "RECOVERING",
        "mock": True,
        "reason": "Test update",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["state"] == "RECOVERING"

    # Reset back to READY
    client.post("/api/codex/quota", json={
        "state": "READY",
        "mock": True,
    })


def test_api_budget_details(client):
    resp = client.get("/api/budget?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "spending" in data
    assert "limits" in data
    assert "reservations" in data


def test_api_policy(client):
    resp = client.get("/api/policy")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "primary_agent" in data
    assert "budget" in data
    assert "models" in data
