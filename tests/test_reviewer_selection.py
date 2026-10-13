"""Independent review selection follows explicit policy, without a fallback."""

import pytest

from config.settings import PolicyConfig
from orchestrator.core import OrchestratorCore


@pytest.mark.parametrize("name,model", [
    ("gemini_cli", "Gemini-CLI"),
    ("antigravity_cli", "Antigravity-CLI"),
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
