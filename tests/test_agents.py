"""Regression checks for safe agent I/O; all model calls are stubbed."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.codex_expert import CodexExpert, CodexQuotaExhaustedError
from agents.flash_api import FlashApiAgent
from agents.gemini_reviewer import GeminiReviewer
from config.settings import BudgetConfig, ModelPricing
from execution.subproc import SubprocResult


@pytest.fixture
def flash() -> FlashApiAgent:
    return FlashApiAgent("glm", ModelPricing(model_id="fixture"), BudgetConfig(), mock_mode=True)


@pytest.mark.parametrize("name", ["../escape.py", "nested/../../escape.py", "/escape.py", "C:\\escape.py", "\\\\host\\share\\escape.py", ".git/config", ".env", "nested/.env/key.py", "file.py:stream", "folder./file.py"])
def test_file_batch_rejects_unsafe_paths_before_any_write(flash, tmp_path, name):
    root = tmp_path / "task"
    root.mkdir()
    body = f"```python:safe.py\nfixture\n```\n```python:{name}\nfixture\n```"
    with pytest.raises(ValueError):
        flash.extract_and_apply_files(body, root)
    assert not (root / "safe.py").exists()


def test_file_batch_accepts_headers_and_fences(flash, tmp_path):
    body = "### File: src/first.py\n```python\nfirst = 1\n```\n```python:second.py\nsecond = 2\n```"
    written = flash.extract_and_apply_files(body, tmp_path)
    assert len(written) == 2
    assert (tmp_path / "src/first.py").read_text() == "first = 1\n"
    assert (tmp_path / "second.py").read_text() == "second = 2\n"


def test_symlink_cannot_redirect_generated_file(flash, tmp_path):
    root = tmp_path / "task"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevation")
    with pytest.raises(ValueError):
        flash.extract_and_apply_files("```python:linked/fixture.py\nfixture\n```", root)
    assert not (outside / "fixture.py").exists()


def test_missing_key_does_not_silently_enable_mock(monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("FLASH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        FlashApiAgent("glm", ModelPricing(model_id="fixture"), BudgetConfig())


def real_agent(monkeypatch, content, usage):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)
    create = AsyncMock(return_value=response)
    constructor = Mock(return_value=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr("agents.flash_api.AsyncOpenAI", constructor)
    agent = FlashApiAgent("deepseek", ModelPricing(model_id="fixture"), BudgetConfig(), api_key="fixture-key")
    return agent, create, constructor


@pytest.mark.asyncio
async def test_api_has_output_limit_no_retries_and_project_context(monkeypatch):
    agent, create, constructor = real_agent(monkeypatch, "fixture", SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    await agent.execute("do fixture", {"project_context": {"source": "fixture source"}})
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert create.call_args.kwargs["max_tokens"] == 4096
    assert "fixture source" in create.call_args.kwargs["messages"][1]["content"]
    assert "fixture-key" not in str(create.call_args.kwargs["messages"])


@pytest.mark.asyncio
async def test_failed_file_application_keeps_paid_usage(monkeypatch, tmp_path):
    agent, _, _ = real_agent(monkeypatch, "```python:../outside.py\nfixture\n```", SimpleNamespace(prompt_tokens=1000, completion_tokens=500))
    answer = await agent.execute("fixture", {"worktree_path": tmp_path})
    assert not answer.success
    assert answer.prompt_tokens == 1000
    assert answer.completion_tokens == 500
    assert answer.cost_cny > 0


@pytest.mark.asyncio
async def test_missing_usage_fails_without_applying_files_and_keeps_reservation(monkeypatch, tmp_path):
    agent, _, _ = real_agent(monkeypatch, "```python:fixture.py\nfixture\n```", None)
    answer = await agent.execute("fixture", {"worktree_path": tmp_path, "reserved_cost_cny": 0.5})
    assert not answer.success
    assert answer.cost_cny == 0.5
    assert not (tmp_path / "fixture.py").exists()


def test_reservation_uses_complete_messages_and_peak_prices(flash):
    flash.model_key = "deepseek"
    short = flash.estimate_max_cost("fixture", {})
    long = flash.estimate_max_cost("fixture", {"project_context": "汉字" * 1000})
    assert long > short
    assert short >= 4096 / 1_000_000 * 1.2 * flash.budget_cfg.usd_to_cny_rate


def review_payload(**overrides):
    return {"approved": True, "severity": "P3", "summary": "fixture", "critical_issues": [], "suggestions": [], **overrides}


@pytest.mark.parametrize("exit_code,output", [(1, ""), (0, "not JSON"), (0, json.dumps(review_payload(approved="true"))), (0, json.dumps(review_payload(severity="invalid"))), (0, json.dumps(review_payload(critical_issues="wrong type")))])
@pytest.mark.asyncio
async def test_reviewer_fails_closed(monkeypatch, tmp_path, exit_code, output):
    command = AsyncMock(return_value=SubprocResult(["fixture"], exit_code, output, "fixture", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    answer = await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert not answer.success
    assert not answer.metadata.get("review", {}).get("approved", False)


@pytest.mark.asyncio
async def test_reviewer_unwraps_cli_response_and_uses_task_plan_mode(monkeypatch, tmp_path):
    output = json.dumps({"response": json.dumps(review_payload())})
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, output, "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    answer = await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert answer.success and answer.metadata["review"]["approved"] is True
    assert command.call_args.kwargs["cwd"] == tmp_path.resolve()
    argv = command.call_args.args[0]
    assert argv[argv.index("--approval-mode") + 1] == "plan"


@pytest.mark.parametrize("message", ["status 429", "rate limit, retry later", "访问频率超限", "weekly limit resets next Monday but still available"])
def test_codex_transient_rate_limit_is_not_weekly_exhaustion(message):
    assert not CodexExpert().check_is_quota_exhausted(message)


@pytest.mark.parametrize("message", ["weekly usage limit exhausted", "You have hit your usage limit. Try again next week.", "周额度已耗尽"])
def test_codex_detects_explicit_exhaustion(message):
    assert CodexExpert().check_is_quota_exhausted(message)


@pytest.mark.asyncio
async def test_codex_is_read_only_in_task_directory(monkeypatch, tmp_path):
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, "fixture guidance", "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path})
    assert answer.success
    assert command.call_args.kwargs["cwd"] == tmp_path.resolve()
    argv = command.call_args.args[0]
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.asyncio
async def test_codex_429_fails_without_quota_downgrade(monkeypatch, tmp_path):
    command = AsyncMock(return_value=SubprocResult(["fixture"], 1, "", "status 429 rate limit", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path})
    assert not answer.success
