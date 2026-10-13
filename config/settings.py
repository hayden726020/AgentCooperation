from pathlib import Path
from typing import Literal, Optional, List
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPricing(BaseModel):
    model_id: str
    base_url: str = ""
    input_price_per_m_usd: float = 0.15
    output_price_per_m_usd: float = 0.50


class BudgetConfig(BaseModel):
    monthly_cny: float = 200.0
    warning_cny: float = 160.0
    pause_new_cny: float = 190.0
    hard_stop_cny: float = 200.0
    task_limit_cny: float = 10.0
    usd_to_cny_rate: float = 7.25


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
