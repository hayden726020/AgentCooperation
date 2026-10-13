from typing import Literal, Optional
from agents.flash_api import FlashApiAgent
from config.settings import PolicyConfig, settings


class SingleEngineSlotEnforcer:
    """
    单引擎槽位守护器：
    严格执行架构铁律：GLM Flash 和 DeepSeek Flash 承担完全同一角色，
    在任何单个任务的完整生命周期中，互斥二选一，绝不允许二者串联或同时驻留内存。
    """

    def __init__(self, policy: PolicyConfig):
        self.policy = policy
        self._active_model_key: Optional[str] = None
        self._active_agent: Optional[FlashApiAgent] = None

    def get_primary_agent(
        self,
        chosen_model: Optional[Literal["glm", "deepseek"]] = None,
        mock_mode: bool = False
    ) -> FlashApiAgent:
        target_key = chosen_model or self.policy.primary_agent
        if target_key not in ["glm", "deepseek"]:
            raise ValueError(f"Invalid primary model '{target_key}'. Must be 'glm' or 'deepseek'.")

        # 若已激活不同模型，拒绝在同一任务中混杂
        if self._active_model_key is not None and self._active_model_key != target_key:
            raise RuntimeError(
                f"Protocol Violation: Active engine is '{self._active_model_key}'. "
                f"Cannot concurrently spawn '{target_key}' in the same task lifecycle! "
                f"GLM and DeepSeek are mutually exclusive."
            )

        if self._active_agent is not None:
            return self._active_agent

        pricing = self.policy.models.get(target_key)
        if not pricing:
            # 默认定价
            from config.settings import ModelPricing
            if target_key == "glm":
                pricing = ModelPricing(
                    model_id="glm-5.3-flash",
                    input_price_per_m_usd=0.15,
                    output_price_per_m_usd=0.50
                )
            else:
                pricing = ModelPricing(
                    model_id="deepseek-flash",
                    input_price_per_m_usd=0.15,
                    output_price_per_m_usd=0.60
                )

        api_key = settings.glm_api_key if target_key == "glm" else settings.deepseek_api_key

        self._active_model_key = target_key
        self._active_agent = FlashApiAgent(
            model_key=target_key,
            pricing=pricing,
            budget_cfg=self.policy.budget,
            api_key=api_key,
            mock_mode=mock_mode
        )
        return self._active_agent

    @property
    def active_model_key(self) -> Optional[str]:
        return self._active_model_key
