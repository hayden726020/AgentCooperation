"""Append-only run event store backing the Agent Run Console.

Events are the single source of truth for live visualization and replay. Each
task writes a JSONL file under ``.runtime/events/<task_id>.jsonl``; the web
console tails that file for Server-Sent Events, so streaming also works when the
orchestrator and the web server run in different processes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from execution.security import redact_and_truncate_evidence

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TERMINAL_EVENT_TYPES = frozenset({"result"})
_MAX_PAYLOAD_STRING = 8000
_SENSITIVE_KEYS = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|credential|authorization|private[_-]?key"
    r"|access[_-]?token|refresh[_-]?token|id[_-]?token|bearer[_-]?token|cookie"
    r"|(?<![a-z0-9_])token(?![a-z0-9_]))"
)

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock so separate orchestrators never reuse a seq."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out locking event file: {path}")
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _redact_value(value: Any, key: Optional[str] = None) -> Any:
    if key is not None and _SENSITIVE_KEYS.search(str(key)):
        # Redact the whole subtree: a sensitive container (credentials,
        # headers) can hold unlabelled secrets on its inner keys.
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_and_truncate_evidence(value, max_chars=_MAX_PAYLOAD_STRING)
    if isinstance(value, dict):
        return {str(item_key): _redact_value(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


@dataclass(frozen=True)
class RunEvent:
    seq: int
    task_id: str
    agent: str
    phase: str
    type: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "task_id": self.task_id,
            "agent": self.agent,
            "phase": self.phase,
            "type": self.type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunEvent":
        return cls(
            seq=int(data["seq"]),
            task_id=str(data["task_id"]),
            agent=str(data.get("agent", "")),
            phase=str(data.get("phase", "")),
            type=str(data.get("type", "")),
            timestamp=str(data.get("timestamp", "")),
            payload=dict(data.get("payload") or {}),
        )

    @property
    def is_terminal(self) -> bool:
        # A paused run still emits a ``result`` to describe the segment, but it
        # can be resumed on the same task_id, so it must not stop the tail.
        return self.type in _TERMINAL_EVENT_TYPES and self.payload.get("terminal", True) is not False


class RunEventEmitter:
    """Best-effort per-run emitter bound to one task_id.

    Observability must never fail the run it observes, so store errors are
    logged and swallowed. ``store=None`` makes every call a no-op.
    """

    def __init__(self, task_id: str, store: Optional["EventStore"] = None) -> None:
        self.task_id = task_id
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.store is not None

    def emit(
        self,
        event_type: str,
        *,
        agent: str = "",
        phase: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[RunEvent]:
        if self.store is None:
            return None
        try:
            return self.store.emit(
                self.task_id,
                event_type,
                agent=agent,
                phase=phase,
                payload=payload or {},
            )
        except Exception:
            logger.exception("Failed to emit run event %s for %s", event_type, self.task_id)
            return None


class EventStore:
    """Thread-safe append-only event store with incremental reads and tailing."""

    def __init__(self, base_dir: Optional[Path | str] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else _PROJECT_ROOT / ".runtime" / "events"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _validate_task_id(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError(f"Invalid task_id for event stream: {task_id!r}")
        return task_id

    def path_for(self, task_id: str) -> Path:
        return self.base_dir / f"{self._validate_task_id(task_id)}.jsonl"

    def emit(
        self,
        task_id: str,
        event_type: str,
        *,
        agent: str = "",
        phase: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> RunEvent:
        """Append one event and return it with its assigned sequence number."""
        self._validate_task_id(task_id)
        clean_payload = _redact_value(payload or {})
        path = self.path_for(task_id)
        with _path_lock(path), _file_lock(path.with_suffix(".lock")):
            seq = self._last_seq(path) + 1
            event = RunEvent(
                seq=seq,
                task_id=task_id,
                agent=str(agent),
                phase=str(phase),
                type=str(event_type),
                timestamp=_utc_now(),
                payload=clean_payload if isinstance(clean_payload, dict) else {"value": clean_payload},
            )
            needs_newline = False
            if path.exists() and path.stat().st_size:
                with path.open("rb") as tail:
                    tail.seek(-1, os.SEEK_END)
                    needs_newline = tail.read(1) != b"\n"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                if needs_newline:
                    handle.write("\n")
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                handle.flush()
            return event

    def read(self, task_id: str, after_seq: int = 0, limit: int = 500) -> list[RunEvent]:
        """Return persisted events with seq greater than ``after_seq``."""
        if limit <= 0:
            return []
        path = self.path_for(task_id)
        if not path.exists():
            return []
        events: list[RunEvent] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = RunEvent.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError):
                    continue
                if event.seq <= after_seq:
                    continue
                events.append(event)
                if len(events) >= limit:
                    break
        return events

    def last_seq(self, task_id: str) -> int:
        """Always read the file: the JSONL log is the only shared source of truth."""
        path = self.path_for(task_id)
        return self._last_seq(path)

    def tail(
        self,
        task_id: str,
        after_seq: int = 0,
        *,
        stop_event: Optional[threading.Event] = None,
        poll_interval: float = 0.2,
        idle_timeout: Optional[float] = None,
        heartbeat_interval: Optional[float] = None,
    ) -> Iterator[Optional[RunEvent]]:
        """Yield events incrementally until terminal, stop flag, or idle timeout.

        Yields ``None`` every ``heartbeat_interval`` seconds while idle so SSE
        handlers can keep the connection alive without inventing events.
        """
        cursor = after_seq
        last_activity = time.monotonic()
        deadline = last_activity + idle_timeout if idle_timeout else None
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            batch = self.read(task_id, after_seq=cursor, limit=200)
            now = time.monotonic()
            if batch:
                for event in batch:
                    cursor = event.seq
                    yield event
                    if event.is_terminal:
                        return
                last_activity = now
                if deadline is not None:
                    deadline = now + idle_timeout
                continue
            if deadline is not None and now >= deadline:
                return
            if heartbeat_interval and now - last_activity >= heartbeat_interval:
                last_activity = now
                yield None
                continue
            time.sleep(poll_interval)

    @staticmethod
    def _last_seq(path: Path) -> int:
        """Return the last valid seq, skipping a corrupt or partially written tail."""
        if not path.exists() or path.stat().st_size == 0:
            return 0
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            chunk = 65536
            buffer = b""
            while size > 0:
                step = min(chunk, size)
                size -= step
                handle.seek(size)
                buffer = handle.read(step) + buffer
                lines = buffer.splitlines()
                # The first line is partial unless we reached the file start.
                for line in reversed(lines if size == 0 else lines[1:]):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        return int(json.loads(line)["seq"])
                    except (ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError):
                        continue
        return 0


event_store = EventStore()
