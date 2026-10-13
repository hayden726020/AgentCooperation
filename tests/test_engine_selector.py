import pytest
from orchestrator.engine_selector import SingleEngineSlotEnforcer
from config.settings import PolicyConfig


def test_single_engine_slot_enforcement():
    policy = PolicyConfig(primary_agent="glm")
    enforcer = SingleEngineSlotEnforcer(policy)

    # 1. 成功获取 GLM 主力 Agent
    glm_agent = enforcer.get_primary_agent("glm", mock_mode=True)
    assert glm_agent.model_key == "glm"
    assert enforcer.active_model_key == "glm"

    # 2. 再次获取 GLM 应当返回同一实例
    glm_agent_2 = enforcer.get_primary_agent("glm", mock_mode=True)
    assert glm_agent_2 is glm_agent

    # 3. 严禁在同一任务生命周期中串联或混入 DeepSeek！应当抛出 RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        enforcer.get_primary_agent("deepseek", mock_mode=True)

    assert "Protocol Violation" in str(exc_info.value)
    assert "mutually exclusive" in str(exc_info.value)


def test_select_deepseek_as_primary():
    policy = PolicyConfig(primary_agent="deepseek")
    enforcer = SingleEngineSlotEnforcer(policy)

    ds_agent = enforcer.get_primary_agent("deepseek", mock_mode=True)
    assert ds_agent.model_key == "deepseek"
    assert enforcer.active_model_key == "deepseek"

    with pytest.raises(RuntimeError):
        enforcer.get_primary_agent("glm", mock_mode=True)
def test_generic_key_and_endpoint_from_settings_are_used(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "glm_api_key", None)
    monkeypatch.setattr(settings, "flash_api_key", "synthetic-test-key")
    monkeypatch.setattr(settings, "flash_base_url", "https://synthetic.invalid/v1")
    agent = SingleEngineSlotEnforcer(PolicyConfig()).get_primary_agent("glm", mock_mode=True)
    assert agent.api_key == "synthetic-test-key"
    assert agent.base_url == "https://synthetic.invalid/v1"
