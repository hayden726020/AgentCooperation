from pathlib import Path
from typing import Literal, Optional, List
import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPricing(BaseModel):
    model_id: str
    base_url: str = ""
    input_price_per_m_usd: float = Field(default=0.15, ge=0, allow_inf_nan=False)
    output_price_per_m_usd: float = Field(default=0.50, ge=0, allow_inf_nan=False)
    peak_input_price_per_m_usd: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    peak_output_price_per_m_usd: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    cache_input_price_per_m_usd: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class BudgetConfig(BaseModel):
    monthly_cny: float = Field(default=200.0, gt=0, allow_inf_nan=False)
    warning_cny: float = Field(default=160.0, ge=0, allow_inf_nan=False)
    pause_new_cny: float = Field(default=190.0, ge=0, allow_inf_nan=False)
    hard_stop_cny: float = Field(default=200.0, gt=0, allow_inf_nan=False)
    task_limit_cny: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    usd_to_cny_rate: float = Field(default=7.25, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def ordered_limits(self):
        if not self.warning_cny <= self.pause_new_cny <= self.hard_stop_cny <= self.monthly_cny:
            raise ValueError('Budget limits must satisfy warning <= pause <= hard stop <= monthly')
        return self


class CodexConfig(BaseModel):
    enabled: bool = True
    min_severity: str = "P1"
    max_calls_per_task: int = 2
    triggers: List[str] = Field(default_factory=lambda: [
        "repeated_failure_3",
        "architecture_conflict",
        "severe_performance_issue",
        "security_issue"
    ])
    fallback: List[str] = Field(default_factory=lambda: ["gemini_cli"])


class RepairConfig(BaseModel):
    max_normal_attempts: int = 3
    verify_with_tests: bool = True
    save_git_checkpoint: bool = True


class ExecutionConfig(BaseModel):
    timeout_seconds: int = 600
    git_worktree_base: str = ".worktrees"
    max_output_tokens: int = Field(default=8192, gt=0)
    max_context_tokens: int = Field(default=200000, gt=0)


class PolicyConfig(BaseModel):
    primary_agent: Literal["glm", "deepseek"] = "glm"
    reviewer_agent: str = "gemini_cli"
    expert_agent: str = "codex_cli"
    models: dict[str, ModelPricing] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # API Keys & Endpoints
    glm_api_key: Optional[str] = Field(default=None, alias="GLM_API_KEY")
    deepseek_api_key: Optional[str] = Field(default=None, alias="DEEPSEEK_API_KEY")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    
    # Generic flash fallback
    flash_api_key: Optional[str] = Field(default=None, alias="FLASH_API_KEY")
    flash_base_url: Optional[str] = Field(default=None, alias="FLASH_BASE_URL")

    # Local CLI command overrides (if different from default PATH commands)
    gemini_cli_cmd: str = "gemini"
    codex_cli_cmd: str = "codex"
    opencode_cli_cmd: str = "opencode"
    gemini_cli_argv_prefix: List[str] = Field(default_factory=list)
    codex_cli_argv_prefix: List[str] = Field(default_factory=list)
    opencode_cli_argv_prefix: List[str] = Field(default_factory=list)

    # SQLite Database
    sqlite_db_path: str = "data/agent_orchestrator.db"
    policy_yaml_path: str = "config/policy.yaml"

    def load_policy(self) -> PolicyConfig:
        policy_path = Path(self.policy_yaml_path)
        if not policy_path.exists():
            return PolicyConfig()

        with open(policy_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        agents_data = raw.get("agents", {})
        models_data = {
            k: ModelPricing(**v) for k, v in raw.get("models", {}).items()
        }
        budget_data = BudgetConfig(**raw.get("budget", {}))
        codex_data = CodexConfig(**raw.get("codex", {}))
        repair_data = RepairConfig(**raw.get("repair", {}))
        exec_data = ExecutionConfig(**raw.get("execution", {}))

        return PolicyConfig(
            primary_agent=agents_data.get("primary", "glm"),
            reviewer_agent=agents_data.get("reviewer", "gemini_cli"),
            expert_agent=agents_data.get("expert", "codex_cli"),
            models=models_data,
            budget=budget_data,
            codex=codex_data,
            repair=repair_data,
            execution=exec_data
        )


settings = AppSettings()
