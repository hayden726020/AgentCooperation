"""Regression checks for safe agent I/O; all model calls are stubbed."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agents.codex_expert import CodexExpert
from agents.flash_api import FlashApiAgent
from agents.gemini_reviewer import GeminiReviewer
from agents.opencode_runner import OpenCodeRunner
from config.settings import settings
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


def test_explanatory_heading_is_not_a_generated_filename(flash, tmp_path):
    assert flash.extract_and_apply_files("### Explanation\n```python\nfixture\n```", tmp_path) == []
    assert not (tmp_path / "Explanation").exists()


def test_duplicate_files_are_rejected_before_writes(flash, tmp_path):
    with pytest.raises(ValueError):
        flash.extract_and_apply_files("```python:fixture.py\na\n```\n```python:fixture.py\nb\n```", tmp_path)
    assert not (tmp_path / "fixture.py").exists()


def test_replacing_a_hardlink_does_not_modify_the_original(flash, tmp_path):
    original = tmp_path / ".env"
    original.write_text("fixture only")
    alias = tmp_path / "fixture.py"
    os.link(original, alias)
    flash.extract_and_apply_files("```python:fixture.py\nnew fixture\n```", tmp_path)
    assert original.read_text() == "fixture only"
    assert alias.read_text() == "new fixture\n"


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


@pytest.mark.skipif(os.name != "nt", reason="Windows junction compatibility regression")
def test_python311_junction_cannot_alias_sensitive_directory(flash, tmp_path, monkeypatch):
    sensitive = tmp_path / ".git"
    sensitive.mkdir()
    alias = tmp_path / "alias"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(sensitive)], capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("Filesystem does not permit temporary directory junctions")
    # Python 3.11 has no Path.is_junction; the resolved target still needs checking.
    monkeypatch.setattr(Path, "is_junction", lambda path: False, raising=False)
    with pytest.raises(ValueError):
        flash.extract_and_apply_files("```python:alias/fixture.py\nfixture\n```", tmp_path)
    assert not (sensitive / "fixture.py").exists()


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
async def test_context_output_limit_is_used_for_the_call_and_reservation(monkeypatch):
    agent, create, _ = real_agent(monkeypatch, "fixture", SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    context = {"max_output_tokens": 8192}
    assert agent.estimate_max_cost("fixture", context) > agent.estimate_max_cost("fixture", {})
    answer = await agent.execute("fixture", context)
    assert answer.success
    assert create.call_args.kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_unknown_api_failure_keeps_cost_and_redacts_key(monkeypatch):
    agent, create, _ = real_agent(monkeypatch, "fixture", None)
    create.side_effect = RuntimeError("fixture-key is invalid")
    answer = await agent.execute("fixture", {"reserved_cost_cny": 0.5})
    assert not answer.success
    assert answer.cost_cny == 0.5
    assert answer.metadata["usage_missing"]
    assert "fixture-key" not in answer.error
    assert create.await_count == 1


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


@pytest.mark.asyncio
async def test_real_review_requires_a_task_directory(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    answer = await GeminiReviewer().execute("fixture")
    assert not answer.success
    command.assert_not_called()


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


@pytest.mark.asyncio
async def test_codex_structured_audit_keeps_rejection(monkeypatch, tmp_path):
    output = json.dumps(review_payload(approved=False, severity="P1"))
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, output, "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "structured_review": True})
    assert answer.success
    assert answer.metadata["review"]["approved"] is False


@pytest.mark.asyncio
async def test_codex_invalid_structured_audit_does_not_pass(monkeypatch, tmp_path):
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, "fixture advice", "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "structured_review": True})
    assert not answer.success


@pytest.mark.asyncio
async def test_review_contains_original_requirement(monkeypatch, tmp_path):
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, json.dumps(review_payload()), "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    requirement = "Implement the full fixture request with persistent state"
    await GeminiReviewer().execute("Review changes", {"worktree_path": tmp_path, "requirement": requirement})
    assert requirement in command.call_args.kwargs["input_text"]


@pytest.mark.asyncio
async def test_gemini_only_receives_its_own_explicit_credential(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fixture")
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, json.dumps(review_payload()), "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert command.call_args.kwargs["env"] == {"GEMINI_API_KEY": "gemini-fixture"}
    assert "gemini-fixture" not in str(command.call_args.args)


@pytest.mark.parametrize("stdout,exit_code", [("b" * 40, 0), ("", 1), ("unexpected", 0)])
@pytest.mark.asyncio
async def test_codex_backlog_verifies_expected_head_before_model(monkeypatch, tmp_path, stdout, exit_code):
    command = AsyncMock(return_value=SubprocResult(["git"], exit_code, stdout, "fixture", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40})
    assert not answer.success
    assert command.await_count == 1
    assert command.call_args.args[0] == ["git", "rev-parse", "HEAD"]


@pytest.mark.asyncio
async def test_codex_matching_head_is_checked_before_subscription_call(monkeypatch, tmp_path):
    sha = "a" * 40
    command = AsyncMock(side_effect=[SubprocResult(["git"], 0, sha + "\n", "", 0), SubprocResult(["git"], 0, "", "", 0), SubprocResult(["codex"], 0, "fixture guidance", "", 0), SubprocResult(["git"], 0, sha, "", 0), SubprocResult(["git"], 0, "", "", 0)])
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "expected_commit_sha": sha})
    assert answer.success
    assert command.await_count == 5
    assert command.call_args_list[0].args[0] == ["git", "rev-parse", "HEAD"]
    assert command.call_args_list[1].args[0] == ["git", "status", "--porcelain", "--untracked-files=all"]
    assert command.call_args_list[2].args[0][0] == "codex"
    assert "OPENAI_API_KEY" not in command.call_args.kwargs.get("env", {})


@pytest.mark.parametrize("output,exit_code", [(" M source.py\n", 0), ("?? new_source.py\n", 0), ("", 1)])
@pytest.mark.asyncio
async def test_codex_backlog_rejects_dirty_or_unreadable_checkout(monkeypatch, tmp_path, output, exit_code):
    sha = "a" * 40
    command = AsyncMock(side_effect=[SubprocResult(["git"], 0, sha, "", 0), SubprocResult(["git"], exit_code, output, "fixture", 0)])
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "expected_commit_sha": sha})
    assert not answer.success
    assert command.await_count == 2
    assert command.call_args_list[1].args[0] == ["git", "status", "--porcelain", "--untracked-files=all"]


@pytest.mark.parametrize("after_sha,status_output,status_exit", [("b" * 40, "", 0), ("a" * 40, " M source.py\n", 0), ("a" * 40, "?? new.py\n", 0), ("a" * 40, "", 1)])
@pytest.mark.asyncio
async def test_codex_rejects_checkout_drift_during_audit(monkeypatch, tmp_path, after_sha, status_output, status_exit):
    sha = "a" * 40
    responses = [
        SubprocResult(["git"], 0, sha, "", 0),
        SubprocResult(["git"], 0, "", "", 0),
        SubprocResult(["codex"], 0, json.dumps(review_payload()), "", 0),
        SubprocResult(["git"], 0, after_sha, "", 0),
        SubprocResult(["git"], status_exit, status_output, "fixture", 0),
    ]
    command = AsyncMock(side_effect=responses)
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    answer = await CodexExpert().execute("fixture", {"worktree_path": tmp_path, "expected_commit_sha": sha, "structured_review": True})
    assert not answer.success
    assert "review" not in answer.metadata
    assert command.await_count >= 4


@pytest.mark.asyncio
async def test_paid_usage_above_reservation_is_retained_and_not_applied(monkeypatch, tmp_path):
    agent, _, _ = real_agent(monkeypatch, "```python:fixture.py\nfixture\n```", SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=500))
    answer = await agent.execute("fixture", {"worktree_path": tmp_path, "reserved_cost_cny": 0.5})
    assert not answer.success
    assert answer.cost_cny > 0.5
    assert answer.metadata["usage_known"]
    assert not (tmp_path / "fixture.py").exists()


@pytest.mark.parametrize("model,expected", [("zai/glm-5.3-flash", {"GLM_API_KEY": "glm-fixture"}), ("deepseek/deepseek-flash", {"DEEPSEEK_API_KEY": "deepseek-fixture"}), ("unknown/fixture", {})])
@pytest.mark.asyncio
async def test_opencode_only_receives_selected_provider_credential(monkeypatch, tmp_path, model, expected):
    monkeypatch.setattr(settings, "glm_api_key", "glm-fixture")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-fixture")
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, "fixture", "", 0))
    monkeypatch.setattr("agents.opencode_runner.run_async_command", command)
    answer = await OpenCodeRunner(model).execute("fixture", {"worktree_path": tmp_path})
    assert answer.success
    assert command.call_args.kwargs["env"] == expected
    assert command.call_args.kwargs["cwd"] == tmp_path.resolve()


@pytest.mark.asyncio
async def test_gemini_failure_diagnostic_redacts_its_credential(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-fixture")
    command = AsyncMock(return_value=SubprocResult(["fixture"], 1, "", "Invalid gemini-fixture credential", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    answer = await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert not answer.success
    assert "gemini-fixture" not in answer.error


@pytest.mark.asyncio
async def test_opencode_failure_diagnostic_redacts_selected_credential(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "glm_api_key", "glm-fixture")
    command = AsyncMock(return_value=SubprocResult(["fixture"], 1, "", "Invalid glm-fixture credential", 0))
    monkeypatch.setattr("agents.opencode_runner.run_async_command", command)
    answer = await OpenCodeRunner("zai/glm-5.3-flash").execute("fixture", {"worktree_path": tmp_path})
    assert not answer.success
    assert "glm-fixture" not in answer.error
