from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import orchestrator.cli as cli
from config.settings import settings


def test_failed_cli_task_returns_nonzero(monkeypatch):
    monkeypatch.setattr("sys.argv", ["orchestrator", "run", "--task", "test", "--mock"])
    monkeypatch.setattr(cli, "execute_task", AsyncMock(return_value=SimpleNamespace(state="FAILED")))
    assert cli.main() == 2


def test_cli_accepts_target_repository(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["orchestrator", "run", "--task", "test", "--repo-root", str(tmp_path), "--mock"])
    execute = AsyncMock(return_value=SimpleNamespace(state="SUCCESS"))
    monkeypatch.setattr(cli, "execute_task", execute)
    assert cli.main() == 0
    assert execute.call_args.kwargs["repo_root"] == tmp_path


def test_mock_core_database_is_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "sqlite_db_path", str(tmp_path / "actual.db"))
    with cli._core_scope(mock=True, repo_root=tmp_path) as core:
        assert Path(core.session.get_bind().url.database).name == "actual.mock.db"
    assert not (tmp_path / "actual.db").exists()


@pytest.mark.asyncio
async def test_watch_does_not_audit_before_verified_recovery(monkeypatch):
    guard = SimpleNamespace(get_pending_audits=lambda: [object()],
                            probe_recovery=AsyncMock(return_value=False),
                            process_backlog=AsyncMock())
    @contextmanager
    def scope(**kwargs):
        yield SimpleNamespace(codex_guard=guard)
    monkeypatch.setattr(cli, "_core_scope", scope, raising=False)
    await cli.watch_codex_backlog(interval=30, cycles=1)
    guard.process_backlog.assert_not_awaited()
