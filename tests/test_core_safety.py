from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import orchestrator.core as core_module
from agents.base import AgentResult
from agents.codex_expert import CodexQuotaExhaustedError
from execution.test_runner import TestResult as RunResult
from storage.models import PendingCodexAudit, Task


@pytest.fixture
def harness(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    monkeypatch.setattr(core_module, "init_db", lambda *args, **kwargs: None)
    primary = SimpleNamespace(model_name="fake-flash", mock_mode=True)

    async def generate(prompt, context=None):
        root = Path(context["worktree_path"])
        (root / "solution.py").write_text("def solve(): return True\n")
        (root / "test_solution.py").write_text(
            "from solution import solve\ndef test_solve(): assert solve()\n"
        )
        return AgentResult(True, "plan and implementation", 20, 10, 0.01,
                           metadata={"mock": True, "usage_known": True,
                                     "files_written": [str(root / "solution.py"), str(root / "test_solution.py")]})

    primary.execute = AsyncMock(side_effect=generate)
    primary.estimate_max_cost = lambda *args, **kwargs: 0.1
    monkeypatch.setattr(core_module.SingleEngineSlotEnforcer, "get_primary_agent", lambda *args, **kwargs: primary)
    reviewer = SimpleNamespace(execute=AsyncMock(return_value=AgentResult(
        True, "approved", metadata={"review": {"approved": True, "severity": "P3"}})))
    expert = SimpleNamespace(execute=AsyncMock(return_value=AgentResult(True, "validated expert advice")))
    monkeypatch.setattr(core_module, "GeminiReviewer", lambda **kwargs: reviewer)
    monkeypatch.setattr(core_module, "CodexExpert", lambda **kwargs: expert)
    runner = SimpleNamespace(calls=0, outcomes=[True])

    async def test_run(*args, **kwargs):
        idx = min(runner.calls, len(runner.outcomes) - 1)
        passed = runner.outcomes[idx]
        runner.calls += 1
        return RunResult(passed, 1, 0 if passed else 1,
                         "1 passed" if passed else "1 failed", "failure evidence", 0.01)

    runner.run = test_run
    monkeypatch.setattr(core_module, "TestRunner", lambda *args, **kwargs: runner)
    core = core_module.OrchestratorCore(repo_root=tmp_path, session=session)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    core.worktree_mgr = SimpleNamespace(
        create_worktree=AsyncMock(return_value=workspace),
        commit_checkpoint=AsyncMock(return_value="b" * 40),
        get_base_ref=AsyncMock(return_value="a" * 40),
        get_diff=AsyncMock(return_value="diff --git a/solution.py b/solution.py"),
    )
    yield SimpleNamespace(core=core, primary=primary, reviewer=reviewer,
                          expert=expert, runner=runner, session=session)
    session.close()
    engine.dispose()


async def run(harness):
    return await harness.core.run_task("Implement solve with tests", mock_mode=True)


@pytest.mark.asyncio
async def test_failed_developer_cannot_deliver(harness):
    harness.primary.execute.side_effect = None
    harness.primary.execute.return_value = AgentResult(False, "", error="upstream unavailable")
    task = await run(harness)
    assert task.state == "FAILED"
    assert harness.runner.calls == 0


@pytest.mark.asyncio
async def test_review_transport_failure_cannot_deliver(harness):
    harness.reviewer.execute.return_value = AgentResult(False, "", error="login expired")
    assert (await run(harness)).state != "SUCCESS"


@pytest.mark.asyncio
async def test_review_rejection_is_repaired_and_retested(harness):
    harness.reviewer.execute.side_effect = [
        AgentResult(True, "boundary bug", metadata={"review": {"approved": False, "severity": "P2"}}),
        AgentResult(True, "approved", metadata={"review": {"approved": True, "severity": "P3"}}),
    ]
    task = await run(harness)
    assert task.state == "SUCCESS"
    assert harness.runner.calls == 2
    assert task.repair_attempts == 1


@pytest.mark.asyncio
async def test_codex_patch_must_pass_tests_and_review(harness):
    harness.runner.outcomes = [False, False, False, False, True]
    task = await run(harness)
    assert task.state == "SUCCESS"
    assert harness.runner.calls == 5
    assert harness.reviewer.execute.await_count == 1


@pytest.mark.asyncio
async def test_exhausted_codex_cannot_override_failing_tests(harness):
    harness.runner.outcomes = [False]
    harness.expert.execute.side_effect = CodexQuotaExhaustedError("weekly usage limit")
    task = await run(harness)
    assert task.state != "SUCCESS"
    assert task.has_pending_codex_audit


@pytest.mark.asyncio
async def test_p0_is_preserved_and_requires_approval(harness):
    harness.reviewer.execute.return_value = AgentResult(
        True, "data loss risk", metadata={"review": {"approved": False, "severity": "P0"}})
    harness.expert.execute.side_effect = CodexQuotaExhaustedError("weekly usage limit")
    task = await run(harness)
    assert task.state == "WAITING_APPROVAL"
    audit = harness.session.exec(select(PendingCodexAudit)).one()
    assert audit.severity == "P0"
    assert harness.primary.execute.await_count == 2


@pytest.mark.asyncio
async def test_codex_transport_failure_cannot_apply_patch(harness):
    harness.runner.outcomes = [False]
    harness.expert.execute.return_value = AgentResult(False, "", error="authentication failed")
    task = await run(harness)
    assert task.state != "SUCCESS"
    assert harness.primary.execute.await_count == 5


@pytest.mark.asyncio
async def test_disabled_codex_is_not_called(harness):
    harness.core.policy.codex.enabled = False
    harness.runner.outcomes = [False]
    assert (await run(harness)).state != "SUCCESS"
    harness.expert.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_development_receives_plan_and_project_context(harness):
    assert (await run(harness)).state == "SUCCESS"
    prompt, = harness.primary.execute.await_args_list[1].args
    assert "plan and implementation" in prompt
    assert "project_context" in harness.primary.execute.await_args_list[1].kwargs["context"]


@pytest.mark.asyncio
async def test_workspace_setup_failure_is_persisted(harness):
    harness.core.worktree_mgr.create_worktree.side_effect = RuntimeError("git worktree denied")
    task = await run(harness)
    assert task.state == "FAILED"
    assert "worktree denied" in task.result_summary
    assert harness.session.exec(select(Task)).one().state == "FAILED"
