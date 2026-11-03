"""Tests for run event APIs and Server-Sent Events in the Web Console."""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from config.settings import PROJECT_ROOT, AppSettings, settings
from execution.events import EventStore
from storage.models import Task
from web import run_service
from web.app import create_app


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "events")


@pytest.fixture
def app(store, monkeypatch):
    monkeypatch.setattr("web.api.event_store", store)
    app = create_app(mock_default=True)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def run_db(monkeypatch):
    """Route run list/detail queries to an isolated in-memory ledger."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    @contextmanager
    def _session():
        yield session

    class _Core:
        def _session(self):
            return _session()

    @contextmanager
    def _scope(mock=False, repo_root=None):
        yield _Core()

    monkeypatch.setattr("web.api._core_scope", _scope)
    yield session
    session.close()
    engine.dispose()


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                fields["id"] = int(line[4:])
            elif line.startswith("event: "):
                fields["event"] = line[7:]
            elif line.startswith("data: "):
                fields["data"] = json.loads(line[6:])
        if fields:
            events.append(fields)
    return events


def test_run_events_endpoint_returns_incremental_events(client, store):
    store.emit("task_1", "status", payload={"state": "RUNNING"})
    store.emit("task_1", "output", payload={"text": "a"})
    store.emit("task_1", "output", payload={"text": "b"})

    resp = client.get("/api/runs/task_1/events?after=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [event["seq"] for event in data["events"]] == [2, 3]
    assert data["last_seq"] == 3


def test_run_events_endpoint_respects_limit(client, store):
    for index in range(5):
        store.emit("task_1", "output", payload={"index": index})

    resp = client.get("/api/runs/task_1/events?limit=2")
    assert [event["seq"] for event in resp.get_json()["events"]] == [1, 2]


@pytest.mark.parametrize("query", ["after=-1", "after=abc", "limit=0"])
def test_run_events_endpoint_rejects_invalid_query(client, query):
    resp = client.get(f"/api/runs/task_1/events?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


@pytest.mark.parametrize("task_id", ["..%2Fescape", "a%2Fb"])
def test_run_events_endpoint_rejects_path_traversal(client, task_id):
    resp = client.get(f"/api/runs/{task_id}/events")
    assert resp.status_code == 404


@pytest.mark.parametrize("task_id", [".hidden", "-flag"])
def test_run_events_endpoint_rejects_invalid_task_id(client, task_id):
    resp = client.get(f"/api/runs/{task_id}/events")
    assert resp.status_code == 400


def test_sse_streams_events_in_order_and_stops_on_result(client, store):
    store.emit("task_1", "status", payload={"state": "RUNNING"})
    store.emit("task_1", "output", payload={"text": "working"})
    store.emit("task_1", "result", payload={"status": "SUCCESS"})

    resp = client.get("/api/runs/task_1/events/stream?idle_timeout=0.1")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"

    events = _parse_sse(resp.get_data(as_text=True))
    assert [event["id"] for event in events] == [1, 2, 3]
    assert [event["event"] for event in events] == ["status", "output", "result"]
    assert events[-1]["data"]["payload"] == {"status": "SUCCESS"}


def test_sse_resumes_from_last_event_id(client, store):
    store.emit("task_1", "status")
    store.emit("task_1", "output", payload={"text": "working"})
    store.emit("task_1", "result", payload={"status": "SUCCESS"})

    resp = client.get(
        "/api/runs/task_1/events/stream?idle_timeout=0.1",
        headers={"Last-Event-ID": "2"},
    )
    events = _parse_sse(resp.get_data(as_text=True))
    assert [event["id"] for event in events] == [3]


def test_sse_with_after_query_overrides_last_event_id(client, store):
    store.emit("task_1", "status")
    store.emit("task_1", "result", payload={"status": "FAILED"})

    resp = client.get(
        "/api/runs/task_1/events/stream?after=2&idle_timeout=0.1",
        headers={"Last-Event-ID": "1"},
    )
    assert _parse_sse(resp.get_data(as_text=True)) == []


def test_sse_empty_stream_ends_after_idle_timeout(client):
    started = time.monotonic()
    resp = client.get("/api/runs/task_1/events/stream?idle_timeout=0.1")
    body = resp.get_data(as_text=True)
    assert body == "retry: 3000\n\n"
    assert time.monotonic() - started < 2.0


def test_sse_sends_heartbeat_comments_while_idle(client, store):
    store.emit("task_1", "status", payload={"state": "RUNNING"})

    resp = client.get(
        "/api/runs/task_1/events/stream?after=1&idle_timeout=0.3&heartbeat=0.05"
    )
    body = resp.get_data(as_text=True)
    assert ": ping" in body
    assert "id: 2" not in body


def test_sse_returns_204_when_terminal_already_consumed(client, store):
    store.emit("task_1", "status")
    store.emit("task_1", "result", payload={"status": "SUCCESS"})

    started = time.monotonic()
    resp = client.get("/api/runs/task_1/events/stream?after=2&idle_timeout=30")
    assert resp.status_code == 204
    assert time.monotonic() - started < 1.0


def test_sse_sanitizes_event_type_into_single_field(client, store):
    store.emit("task_1", "output\nid: 999", payload={"text": "x"})

    resp = client.get("/api/runs/task_1/events/stream?idle_timeout=0.1")
    events = _parse_sse(resp.get_data(as_text=True))
    assert [event["id"] for event in events] == [1]
    assert events[0]["event"] == "output_id__999"


def test_sse_rejects_invalid_task_id(client):
    resp = client.get("/api/runs/.hidden/events/stream?idle_timeout=0.1")
    assert resp.status_code == 400


def test_sse_path_traversal_is_not_routed(client):
    resp = client.get("/api/runs/..%2Fescape/events/stream?idle_timeout=0.1")
    assert resp.status_code == 404


@pytest.mark.parametrize("query", ["after=abc", "idle_timeout=abc"])
def test_sse_rejects_invalid_query(client, query):
    resp = client.get(f"/api/runs/task_1/events/stream?{query}")
    assert resp.status_code == 400


def _policy(allowed_repos, max_attempts=3):
    return SimpleNamespace(
        security=SimpleNamespace(allowed_repos=list(allowed_repos)),
        repair=SimpleNamespace(max_normal_attempts=max_attempts),
    )


def test_run_options_exposes_reviewers_and_repo_whitelist(client):
    resp = client.get("/api/runs/options")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reviewers"] == ["glm", "deepseek"]
    assert data["default_reviewer"] == "glm"
    assert data["default_repo"]
    assert isinstance(data["repos"], list)
    assert "allowed_repo_patterns" in data


def test_submit_run_applies_smart_defaults(tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    policy = _policy([str(repo)], max_attempts=2)
    dispatched = {}
    monkeypatch.setattr(
        run_service.job_manager,
        "run_async",
        lambda job_id, func: dispatched.setdefault("job_id", job_id),
    )

    info = run_service.submit_run(
        requirement="实现登录页",
        project_root=tmp_path,
        policy=policy,
        repo=str(repo),
    )

    assert info["reviewer"] == "glm"
    assert info["max_repair"] == 2
    assert info["repo"] == str(repo.resolve())
    assert info["task_id"].startswith("task_gemini_")
    assert dispatched["job_id"] == info["job_id"]


def test_submit_run_rejects_repo_outside_whitelist(tmp_path):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    (allowed / ".git").mkdir(parents=True)
    (other / ".git").mkdir(parents=True)
    policy = _policy([str(allowed)])

    with pytest.raises(PermissionError):
        run_service.submit_run(
            requirement="x",
            project_root=tmp_path,
            policy=policy,
            repo=str(other),
        )


def test_submit_run_supports_glob_whitelist(tmp_path, monkeypatch):
    repo = tmp_path / "workplace" / "NAILONG"
    (repo / ".git").mkdir(parents=True)
    policy = _policy([str(tmp_path / "workplace" / "*")])
    monkeypatch.setattr(run_service.job_manager, "run_async", lambda job_id, func: None)

    info = run_service.submit_run(
        requirement="x",
        project_root=tmp_path,
        policy=policy,
        repo="workplace/NAILONG",
    )
    assert info["repo"] == str(repo.resolve())


def test_create_run_validates_request(client):
    assert client.post("/api/runs", json={}).status_code == 400
    assert client.post("/api/runs", json={"requirement": "x", "reviewer": "gpt"}).status_code == 400
    assert client.post("/api/runs", json={"requirement": "x", "mock": "false"}).status_code == 400
    assert client.post("/api/runs", json={"requirement": "x", "max_repair": 99}).status_code == 400


@pytest.mark.parametrize("payload", [
    {"requirement": ["x"]},
    {"requirement": "x", "repo": ["y"]},
    {"requirement": "x", "reviewer": {"name": "glm"}},
    {"requirement": "x", "title": 42},
])
def test_create_run_rejects_wrong_field_types(client, payload):
    resp = client.post("/api/runs", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_create_run_rejects_untrusted_tests(client):
    resp = client.post(
        "/api/runs",
        json={"requirement": "x", "allow_untrusted_tests": True},
    )
    assert resp.status_code == 403
    assert "not permitted" in resp.get_json()["error"]


def test_create_run_rejects_repo_outside_whitelist(client, tmp_path, monkeypatch):
    other = tmp_path / "outside"
    (other / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        AppSettings,
        "load_policy",
        lambda self: _policy([str(PROJECT_ROOT / "workplace")]),
    )
    resp = client.post("/api/runs", json={"requirement": "x", "repo": str(other), "mock": True})
    assert resp.status_code == 403


def test_create_run_queues_with_defaults(client, monkeypatch):
    captured = {}

    def fake_submit(**kwargs):
        captured.update(kwargs)
        return {
            "task_id": "task_gemini_test",
            "job_id": "job_test",
            "repo": str(PROJECT_ROOT),
            "reviewer": "glm",
            "max_repair": 3,
            "mock": True,
        }

    monkeypatch.setattr("web.api.run_service.submit_run", fake_submit)
    resp = client.post("/api/runs", json={"requirement": "实现登录页", "mock": True})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["success"] is True
    assert body["task_id"] == "task_gemini_test"
    assert captured["reviewer"] is None
    assert captured["max_repair"] is None
    assert captured["allow_untrusted_tests"] is False
    assert captured["mock"] is True


def test_list_runs_returns_only_closed_loop_tasks(client, run_db):
    run_db.add(Task(
        task_id="task_gemini_1",
        title="closed loop run",
        requirement="r",
        primary_model=run_service.CLOSED_LOOP_MODEL,
        state="SUCCESS",
        mock_mode=True,
    ))
    run_db.add(Task(
        task_id="task_other",
        title="other pipeline",
        requirement="r",
        primary_model="glm-5.3-flashx",
        state="SUCCESS",
    ))
    run_db.commit()

    resp = client.get("/api/runs?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [run["task_id"] for run in data["runs"]] == ["task_gemini_1"]
    assert data["runs"][0]["state"] == "SUCCESS"


def test_get_run_detail_includes_terminal_event(client, run_db, store):
    run_db.add(Task(
        task_id="task_gemini_1",
        title="closed loop run",
        requirement="r",
        primary_model=run_service.CLOSED_LOOP_MODEL,
        state="SUCCESS",
        mock_mode=True,
    ))
    run_db.commit()
    store.emit("task_gemini_1", "status", payload={"state": "REVIEWING"})
    store.emit("task_gemini_1", "result", payload={"state": "SUCCESS"})

    resp = client.get("/api/runs/task_gemini_1?mock=true")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["run"]["task_id"] == "task_gemini_1"
    assert data["events"]["last_seq"] == 2
    assert data["events"]["terminal"]["type"] == "result"


def test_get_run_detail_returns_404(client, run_db):
    resp = client.get("/api/runs/missing_task?mock=true")
    assert resp.status_code == 404


def test_create_run_executes_closed_loop_and_streams_events(client, tmp_path, store, monkeypatch):
    """Submit through the API and verify a real mock-mode closed loop reaches a terminal event."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n.test_sandbox/\n", encoding="utf-8")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    policy = settings.load_policy()
    policy.security.allowed_repos = [str(repo)]
    policy.repair.max_normal_attempts = 1
    monkeypatch.setattr(AppSettings, "load_policy", lambda self: policy)

    resp = client.post("/api/runs", json={
        "requirement": "Add a helper",
        "repo": str(repo),
        "mock": True,
    })
    assert resp.status_code == 202
    task_id = resp.get_json()["task_id"]

    deadline = time.monotonic() + 60
    events = []
    while time.monotonic() < deadline:
        events = store.read(task_id)
        if events and events[-1].is_terminal:
            break
        time.sleep(0.1)

    assert events and events[-1].type == "result"
    assert events[-1].payload["state"] == "SUCCESS", events[-1].payload.get("summary")
    assert any(event.type == "output" for event in events)
    assert any(event.type == "test_line" for event in events)


def _add_task(run_db, task_id, **overrides):
    fields = {
        "task_id": task_id,
        "title": "closed loop run",
        "requirement": "r",
        "primary_model": run_service.CLOSED_LOOP_MODEL,
        "state": "WAITING_APPROVAL",
        "mock_mode": True,
        "repo_root": str(PROJECT_ROOT),
        "reviewer_model": "glm",
    }
    fields.update(overrides)
    run_db.add(Task(**fields))
    run_db.commit()


def test_list_runs_filters_by_repo_root(client, run_db, tmp_path):
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _add_task(run_db, "task_repo_a", state="SUCCESS", repo_root=str(repo_a))
    _add_task(run_db, "task_repo_b", state="SUCCESS", repo_root=str(repo_b))
    _add_task(run_db, "task_no_repo", state="SUCCESS", repo_root=None)

    resp = client.get(f"/api/runs?mock=true&repo={repo_a}")
    assert resp.status_code == 200
    assert [run["task_id"] for run in resp.get_json()["runs"]] == ["task_repo_a"]

    resp_all = client.get("/api/runs?mock=true")
    assert {run["task_id"] for run in resp_all.get_json()["runs"]} == {
        "task_repo_a", "task_repo_b", "task_no_repo",
    }


@pytest.mark.parametrize("action,approved", [("approve", True), ("reject", False)])
def test_approval_endpoint_dispatches_resume(client, run_db, monkeypatch, action, approved):
    captured = {}

    def fake_start_approval(**kwargs):
        captured.update(kwargs)
        return {
            "task_id": kwargs["task_id"],
            "job_id": "job_approval",
            "approved": kwargs["approved"],
            "repo": kwargs["repo_root"],
            "reviewer": "glm",
            "mock": kwargs["mock_mode"],
        }

    monkeypatch.setattr("web.api.run_service.start_approval", fake_start_approval)
    _add_task(run_db, "task_waiting")

    resp = client.post(
        f"/api/runs/task_waiting/{action}?mock=true",
        json={"notes": "reviewed"},
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["success"] is True
    assert body["approved"] is approved
    assert captured["task_id"] == "task_waiting"
    assert captured["approved"] is approved
    assert captured["notes"] == "reviewed"
    assert captured["mock_mode"] is True
    assert captured["repo_root"] == str(PROJECT_ROOT)
    assert captured["reviewer_model"] == "glm"
    assert captured["allow_untrusted_tests"] is False


def test_approval_endpoint_returns_404_for_unknown_run(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    resp = client.post("/api/runs/missing/approve?mock=true", json={})
    assert resp.status_code == 404


def test_approval_endpoint_rejects_non_closed_loop_task(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_other", primary_model="glm-5.3-flashx")
    resp = client.post("/api/runs/task_other/approve?mock=true", json={})
    assert resp.status_code == 400
    assert "closed-loop" in resp.get_json()["error"]


def test_approval_endpoint_rejects_cross_origin(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_approval_endpoint_rejects_ledger_mode_mismatch(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_live", mock_mode=False)
    resp = client.post("/api/runs/task_live/approve?mock=true", json={})
    assert resp.status_code == 409
    assert "mode" in resp.get_json()["error"]


def test_approval_endpoint_rejects_wrong_state(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_done", state="SUCCESS")
    resp = client.post("/api/runs/task_done/approve?mock=true", json={})
    assert resp.status_code == 409
    assert resp.get_json()["state"] == "SUCCESS"


@pytest.mark.parametrize("notes", [123, ["a"], {"b": 1}])
def test_approval_endpoint_rejects_non_string_notes(client, run_db, monkeypatch, notes):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_waiting")
    resp = client.post("/api/runs/task_waiting/approve?mock=true", json={"notes": notes})
    assert resp.status_code == 400


def test_approval_endpoint_rejects_oversized_notes(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={"notes": "x" * 4001},
    )
    assert resp.status_code == 400


def test_approval_endpoint_maps_inflight_conflict(client, run_db, monkeypatch):
    def conflict(**kwargs):
        raise run_service.RunConflictError("already resuming")

    monkeypatch.setattr("web.api.run_service.start_approval", conflict)
    _add_task(run_db, "task_waiting")
    resp = client.post("/api/runs/task_waiting/reject?mock=true", json={})
    assert resp.status_code == 409
    assert "already resuming" in resp.get_json()["error"]


def test_start_approval_guards_concurrent_resume(monkeypatch):
    import threading

    inflight: set[str] = set()
    monkeypatch.setattr(run_service, "_APPROVALS_INFLIGHT", inflight)
    monkeypatch.setattr(run_service, "_APPROVAL_LOCK", threading.Lock())
    monkeypatch.setattr(run_service.job_manager, "create_job", lambda *a, **k: "job_approval")
    monkeypatch.setattr(run_service.job_manager, "run_async", lambda *a, **k: None)

    policy = settings.load_policy()
    policy.security.allowed_repos = [str(PROJECT_ROOT)]

    first = run_service.start_approval(
        task_id="task_conflict",
        approved=True,
        notes=None,
        repo_root=str(PROJECT_ROOT),
        reviewer_model="glm",
        mock_mode=True,
        project_root=PROJECT_ROOT,
        policy=policy,
    )
    assert first["job_id"] == "job_approval"
    assert "task_conflict" in inflight

    with pytest.raises(run_service.RunConflictError):
        run_service.start_approval(
            task_id="task_conflict",
            approved=True,
            notes=None,
            repo_root=str(PROJECT_ROOT),
            reviewer_model="glm",
            mock_mode=True,
            project_root=PROJECT_ROOT,
            policy=policy,
        )


def test_approval_endpoint_rejects_missing_repo_root(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_no_repo", repo_root=None)
    resp = client.post("/api/runs/task_no_repo/approve?mock=true", json={})
    assert resp.status_code == 409
    assert "repository" in resp.get_json()["error"]


def test_approval_endpoint_passes_explicit_untrusted_opt_in(client, run_db, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "task_waiting", "job_id": "job_ok"},
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={"allow_untrusted_tests": True},
    )
    assert resp.status_code == 202
    assert captured["allow_untrusted_tests"] is True


@pytest.mark.parametrize("value", ["true", 1, None])
def test_approval_endpoint_rejects_non_boolean_untrusted_flag(client, run_db, monkeypatch, value):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: pytest.fail("resume must not be dispatched"),
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={"allow_untrusted_tests": value},
    )
    assert resp.status_code == 400


def test_approval_endpoint_allows_rejecting_repo_less_run(client, run_db, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "task_no_repo", "job_id": "job_ok"},
    )
    _add_task(run_db, "task_no_repo", repo_root=None)
    resp = client.post("/api/runs/task_no_repo/reject?mock=true", json={})
    assert resp.status_code == 202
    assert captured["repo_root"] == ""
    assert captured["allow_untrusted_tests"] is False


def test_approval_endpoint_accepts_same_origin(client, run_db, monkeypatch):
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: {"task_id": kwargs["task_id"], "job_id": "job_ok"},
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={},
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 202


def test_approval_endpoint_accepts_notes_at_limit(client, run_db, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "web.api.run_service.start_approval",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "task_waiting", "job_id": "job_ok"},
    )
    _add_task(run_db, "task_waiting")
    resp = client.post(
        "/api/runs/task_waiting/approve?mock=true",
        json={"notes": "x" * 2000},
    )
    assert resp.status_code == 202
    assert captured["notes"] == "x" * 2000


def test_start_approval_reject_skips_stale_repo_resolution(monkeypatch):
    """Rejecting a paused run must not fail just because its repo path moved."""
    import threading

    inflight: set[str] = set()
    monkeypatch.setattr(run_service, "_APPROVALS_INFLIGHT", inflight)
    monkeypatch.setattr(run_service, "_APPROVAL_LOCK", threading.Lock())
    monkeypatch.setattr(run_service.job_manager, "create_job", lambda *a, **k: "job_reject")
    monkeypatch.setattr(run_service.job_manager, "run_async", lambda *a, **k: None)

    policy = settings.load_policy()
    policy.security.allowed_repos = [str(PROJECT_ROOT)]

    info = run_service.start_approval(
        task_id="task_reject_stale",
        approved=False,
        notes="abandon",
        repo_root=str(PROJECT_ROOT / "repo-moved-away"),
        reviewer_model="glm",
        mock_mode=True,
        project_root=PROJECT_ROOT,
        policy=policy,
    )

    assert info["repo"] == str(PROJECT_ROOT.resolve())


def test_start_approval_releases_guard_when_dispatch_fails(monkeypatch):
    import threading

    inflight: set[str] = set()
    monkeypatch.setattr(run_service, "_APPROVALS_INFLIGHT", inflight)
    monkeypatch.setattr(run_service, "_APPROVAL_LOCK", threading.Lock())

    def failing_create(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(run_service.job_manager, "create_job", failing_create)
    policy = settings.load_policy()
    policy.security.allowed_repos = [str(PROJECT_ROOT)]

    with pytest.raises(RuntimeError):
        run_service.start_approval(
            task_id="task_dispatch_fail",
            approved=True,
            notes=None,
            repo_root=str(PROJECT_ROOT),
            reviewer_model="glm",
            mock_mode=True,
            project_root=PROJECT_ROOT,
            policy=policy,
        )
    assert "task_dispatch_fail" not in inflight


def test_start_approval_releases_guard_when_resume_fails(monkeypatch):
    import asyncio
    import threading

    class FailingOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        async def resume_after_approval(self, **kwargs):
            raise RuntimeError("resume exploded")

    inflight: set[str] = set()
    captured = {}
    monkeypatch.setattr(run_service, "_APPROVALS_INFLIGHT", inflight)
    monkeypatch.setattr(run_service, "_APPROVAL_LOCK", threading.Lock())
    monkeypatch.setattr(run_service, "GeminiClosedLoopOrchestrator", FailingOrchestrator)
    monkeypatch.setattr(run_service.job_manager, "create_job", lambda *a, **k: "job_fail")
    monkeypatch.setattr(
        run_service.job_manager,
        "run_async",
        lambda job_id, func: captured.update(func=func),
    )
    policy = settings.load_policy()
    policy.security.allowed_repos = [str(PROJECT_ROOT)]

    run_service.start_approval(
        task_id="task_resume_fail",
        approved=True,
        notes=None,
        repo_root=str(PROJECT_ROOT),
        reviewer_model="glm",
        mock_mode=True,
        project_root=PROJECT_ROOT,
        policy=policy,
    )
    assert "task_resume_fail" in inflight

    with pytest.raises(RuntimeError, match="resume exploded"):
        asyncio.run(captured["func"]())
    assert "task_resume_fail" not in inflight
