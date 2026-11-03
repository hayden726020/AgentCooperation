import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from execution.events import EventStore, RunEvent


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "events")


def test_emit_assigns_monotonic_sequences_and_persists(store):
    first = store.emit("task_1", "status", agent="dev", phase="DEV", payload={"state": "RUNNING"})
    second = store.emit("task_1", "output", agent="dev", phase="DEV", payload={"text": "hello"})

    assert (first.seq, second.seq) == (1, 2)
    assert first.timestamp and first.is_terminal is False
    lines = store.path_for("task_1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["payload"] == {"text": "hello"}


def test_read_after_seq_returns_only_new_events(store):
    for index in range(5):
        store.emit("task_1", "output", payload={"index": index})

    events = store.read("task_1", after_seq=3)
    assert [event.seq for event in events] == [4, 5]
    assert [event.payload["index"] for event in events] == [3, 4]


def test_read_respects_limit(store):
    for index in range(5):
        store.emit("task_1", "output", payload={"index": index})

    assert [event.seq for event in store.read("task_1", limit=2)] == [1, 2]


def test_payload_credentials_are_redacted(store):
    event = store.emit(
        "task_1",
        "output",
        payload={
            "text": "api_key=abcdefgh12345678",
            "nested": ["Authorization: Bearer ya29.secret-token-value"],
        },
    )

    assert "abcdefgh12345678" not in json.dumps(event.payload)
    assert "ya29.secret-token-value" not in json.dumps(event.payload)
    persisted = store.path_for("task_1").read_text(encoding="utf-8")
    assert "abcdefgh12345678" not in persisted
    assert "ya29.secret-token-value" not in persisted


@pytest.mark.parametrize("task_id", ["../escape", "a/b", "a\\b", "", ".hidden", "-flag"])
def test_invalid_task_id_rejected(store, task_id):
    with pytest.raises(ValueError):
        store.emit(task_id, "output")


def test_new_store_instance_continues_sequence(tmp_path):
    base = tmp_path / "events"
    first_store = EventStore(base)
    first_store.emit("task_1", "status")
    first_store.emit("task_1", "status")

    second_store = EventStore(base)
    event = second_store.emit("task_1", "status")
    assert event.seq == 3


def test_new_store_skips_corrupt_final_line_when_assigning_seq(tmp_path):
    base = tmp_path / "events"
    first_store = EventStore(base)
    first_store.emit("task_1", "status")
    with first_store.path_for("task_1").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "task_id": "task_1"')  # partial write, no newline

    second_store = EventStore(base)
    assert second_store.emit("task_1", "status").seq == 2


def test_two_instances_do_not_reuse_sequence_numbers(tmp_path):
    base = tmp_path / "events"
    first_store = EventStore(base)
    second_store = EventStore(base)

    assert first_store.emit("task_1", "status").seq == 1
    assert second_store.emit("task_1", "status").seq == 2
    assert first_store.emit("task_1", "status").seq == 3
    assert first_store.last_seq("task_1") == 3


def test_sensitive_mapping_keys_are_redacted(store):
    event = store.emit(
        "task_1",
        "output",
        payload={
            "api_key": "abcdefgh12345678",
            "nested": {"Authorization": "Bearer abcdefgh12345678"},
            "safe": "visible",
        },
    )

    dumped = json.dumps(event.payload)
    assert "abcdefgh12345678" not in dumped
    assert event.payload["api_key"] == "[REDACTED]"
    assert event.payload["safe"] == "visible"


def test_sensitive_containers_are_fully_redacted(store):
    event = store.emit(
        "task_1",
        "tool_call",
        payload={
            "credentials": {"user": "bob", "value": "hunter2hunter2"},
            "headers": {"Cookie": "session=hunter2", "Accept": "json"},
        },
    )

    assert event.payload["credentials"] == "[REDACTED]"
    assert event.payload["headers"]["Cookie"] == "[REDACTED]"
    assert event.payload["headers"]["Accept"] == "json"
    persisted = store.path_for("task_1").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in persisted
    assert "session=hunter2" not in persisted


def test_google_api_key_is_redacted(store):
    key = "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"[:35]
    event = store.emit("task_1", "output", payload={"text": f"key={key}"})

    assert key not in json.dumps(event.payload)
    assert "[REDACTED_GOOGLE_API_KEY]" in event.payload["text"]


def test_tail_emits_heartbeats_while_idle(store):
    events = list(
        store.tail("task_1", poll_interval=0.01, idle_timeout=0.25, heartbeat_interval=0.05)
    )
    assert events and all(event is None for event in events)


def test_corrupt_lines_are_ignored(store):
    store.emit("task_1", "status", payload={"ok": True})
    path = store.path_for("task_1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json}\n")
    store.emit("task_1", "status", payload={"ok": False})

    events = store.read("task_1")
    assert [event.seq for event in events] == [1, 2]


def test_tail_streams_new_events_until_terminal(store):
    stop = threading.Event()
    received = []

    def writer():
        time.sleep(0.05)
        store.emit("task_1", "status", payload={"state": "RUNNING"})
        store.emit("task_1", "output", payload={"text": "working"})
        store.emit("task_1", "result", payload={"status": "SUCCESS"})

    thread = threading.Thread(target=writer)
    thread.start()
    for event in store.tail("task_1", poll_interval=0.01, idle_timeout=2.0, stop_event=stop):
        received.append(event)
    thread.join()

    assert [event.type for event in received] == ["status", "output", "result"]


def test_paused_result_is_not_terminal(store):
    paused = store.emit(
        "task_1",
        "result",
        payload={"state": "WAITING_APPROVAL", "terminal": False},
    )
    assert paused.is_terminal is False

    terminal = store.emit("task_1", "result", payload={"state": "SUCCESS"})
    assert terminal.is_terminal is True


def test_tail_continues_after_paused_result(store):
    store.emit("task_1", "result", payload={"state": "WAITING_APPROVAL", "terminal": False})
    received = []

    def writer():
        time.sleep(0.05)
        store.emit("task_1", "status", payload={"state": "DEVELOPING"})
        store.emit("task_1", "result", payload={"state": "SUCCESS"})

    thread = threading.Thread(target=writer)
    thread.start()
    for event in store.tail("task_1", after_seq=1, poll_interval=0.01, idle_timeout=2.0):
        received.append(event)
    thread.join()

    assert [event.type for event in received] == ["status", "result"]
    assert received[-1].is_terminal is True


def test_tail_resumes_after_sequence(store):
    store.emit("task_1", "status")
    store.emit("task_1", "status")
    store.emit("task_1", "result")

    events = list(store.tail("task_1", after_seq=2, poll_interval=0.01, idle_timeout=1.0))
    assert [event.seq for event in events] == [3]


def test_tail_honors_stop_event(store):
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    assert list(store.tail("task_1", poll_interval=0.01, stop_event=stop)) == []
    assert time.monotonic() - started < 1.0


def test_tail_idle_timeout(store):
    started = time.monotonic()
    assert list(store.tail("task_1", poll_interval=0.01, idle_timeout=0.1)) == []
    assert time.monotonic() - started < 2.0


def test_cross_process_emits_have_unique_sequences(tmp_path):
    base = tmp_path / "events"
    script = (
        "import sys\n"
        "from execution.events import EventStore\n"
        "store = EventStore(sys.argv[1])\n"
        "for _ in range(5):\n"
        "    store.emit('task_1', 'output')\n"
    )
    project_root = Path(__file__).resolve().parents[1]
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(base)], cwd=project_root)
        for _ in range(3)
    ]
    for process in processes:
        assert process.wait(timeout=60) == 0

    events = EventStore(base).read("task_1", limit=100)
    assert [event.seq for event in events] == list(range(1, 16))


def test_read_tolerates_non_object_and_invalid_utf8_lines(store):
    store.emit("task_1", "status")
    path = store.path_for("task_1")
    with path.open("ab") as handle:
        handle.write(b'null\n[1, 2]\n{"seq": 1e400}\n\xff\xfe\n')
    store.emit("task_1", "status")

    assert [event.seq for event in store.read("task_1", limit=10)] == [1, 2]


def test_concurrent_emits_have_unique_sequences(store):
    threads = [
        threading.Thread(target=lambda: store.emit("task_1", "output", payload={"thread": "x"}))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = store.read("task_1", limit=100)
    assert len(events) == 20
    assert sorted(event.seq for event in events) == list(range(1, 21))


def test_run_event_round_trip():
    event = RunEvent(1, "task_1", "dev", "DEV", "output", "2026-10-18T00:00:00+00:00", {"text": "x"})
    assert RunEvent.from_dict(event.to_dict()) == event
