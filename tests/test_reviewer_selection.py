"""Independent review selection follows explicit policy, without a fallback."""

import pytest

from config.settings import PolicyConfig
from orchestrator.core import OrchestratorCore


@pytest.mark.parametrize("name,model", [
    ("gemini_cli", "Gemini-CLI"),
    ("antigravity_cli", "Antigravity-CLI"),
    ("codex_cli", "Codex-CLI"),
])
def test_reviewer_factory_honors_policy(name, model):
    core = object.__new__(OrchestratorCore)
    core.policy = PolicyConfig(reviewer_agent=name)
    reviewer = core._get_reviewer(mock_mode=True)
    assert reviewer.model_name == model
    assert reviewer.mock_mode is True


def test_unknown_reviewer_configuration_is_rejected():
    with pytest.raises(ValueError):
        PolicyConfig(reviewer_agent="unavailable_provider")


def test_default_reviewer_uses_available_subscription_route():
    from config.settings import settings
    assert PolicyConfig().reviewer_agent == "antigravity_cli"
    assert settings.load_policy().reviewer_agent == "antigravity_cli"


def test_default_antigravity_model_matches_selected_effort():
    from config.settings import AppSettings
    defaults = AppSettings(_env_file=None)
    assert defaults.antigravity_review_model == "gemini-3.8-flash-high"
    assert defaults.antigravity_review_effort == "high"
    assert defaults.codex_expert_model == "gpt-6.1-sol"


@pytest.mark.asyncio
async def test_codex_reviewer_forwards_all_evidence_and_binds_commit(monkeypatch, tmp_path):
    from agents.base import AgentResult
    from agents.codex_expert import CodexExpert
    from unittest.mock import AsyncMock
    execute = AsyncMock(return_value=AgentResult(True, "fixture", metadata={"review": {"approved": True}}))
    monkeypatch.setattr(CodexExpert, "execute", execute)
    core = object.__new__(OrchestratorCore)
    core.policy = PolicyConfig(reviewer_agent="codex_cli")
    reviewer = core._get_reviewer(mock_mode=False)
    context = {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40,
               "requirement": "original requirement", "git_diff": "verified diff", "test_summary": "2 passed"}
    answer = await reviewer.execute("Review independently", context)
    assert answer.success
    forwarded = execute.call_args.args[1]
    assert forwarded["structured_review"] is True
    assert forwarded["expected_commit_sha"] == "a" * 40
    assert all(context[key] in forwarded["issue_context"] for key in ("requirement", "git_diff", "test_summary"))


@pytest.mark.asyncio
async def test_codex_reviewer_requires_commit(monkeypatch, tmp_path):
    from agents.codex_expert import CodexExpert
    from unittest.mock import AsyncMock
    execute = AsyncMock()
    monkeypatch.setattr(CodexExpert, "execute", execute)
    core = object.__new__(OrchestratorCore)
    core.policy = PolicyConfig(reviewer_agent="codex_cli")
    answer = await core._get_reviewer(mock_mode=False).execute("Review", {"worktree_path": tmp_path})
    assert not answer.success
    execute.assert_not_called()
