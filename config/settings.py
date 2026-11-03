import os
import re
from pathlib import Path
from typing import Dict, Literal, Optional, List
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure .env is loaded into process environment regardless of startup CWD
_default_env = Path(os.getenv("AGENT_COOPERATION_ENV_FILE") or (PROJECT_ROOT / ".env"))
if _default_env.is_file():
    load_dotenv(dotenv_path=_default_env, override=False)


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


COMMAND_CATEGORIES = ("test", "build", "typecheck")
_MAX_COMMANDS_PER_CATEGORY = 32
_MAX_ARGUMENTS_PER_COMMAND = 64
_MAX_ARGUMENT_LENGTH = 256
_FORBIDDEN_TOKEN_FRAGMENTS = (
    "\x00", "\n", "\r", "&&", "||", ";", "|", ">", "<", "`", "$",
    "\\", '"', "'", "*", "?", "[", "]", "{", "}", "~",
)
_PROTECTED_COMMAND_PARTS = frozenset({
    ".git", ".env", ".aws", ".codex", ".agents", ".integration", ".runtime", ".worktrees", "data",
})
_BLOCKED_COMMAND_FILES = frozenset({
    "credentials", "credentials.json", "token.json", "tokens.json", "auth.json",
    "secrets.json", "secret.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "known_hosts", "authorized_keys",
})
_BLOCKED_COMMAND_EXTENSIONS = (".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore", ".jks")
_SHELL_EXECUTABLES = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "cmd", "powershell", "pwsh", "env", "xargs", "su", "sudo", "runas",
    "wsl", "busybox", "expect", "script",
})
_MIN_ARGUMENTS_FOR_EXECUTABLES = frozenset({
    "python", "python3", "py", "node", "nodejs", "ruby", "perl", "php", "java",
    "dotnet", "npm", "npx", "yarn", "pnpm", "bun", "deno", "cargo", "go", "make",
    "mix", "stack",
})
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_REPO_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_FORBIDDEN_COMMAND_OPTIONS = frozenset({
    "--rootdir", "--confcutdir", "--basetemp", "--pyargs", "--override-ini",
    "--script-shell", "--shell", "--package", "--call", "--registry",
    "--userconfig", "--globalconfig", "--prefix", "--workspace", "--directory",
})
_COMMON_FORBIDDEN_SHORT_OPTIONS = frozenset({"-C", "-g", "-w"})
_PYTEST_FORBIDDEN_OPTIONS = frozenset({"-c", "-o"})
_PYTEST_FORBIDDEN_LONG_OPTIONS = frozenset({"--config-file"})
_PYTEST_VALUE_TAKING_SHORT = frozenset("kmpWrn")


def _has_combined_pytest_override(token: str) -> bool:
    """Detect `-qc file` style combinations; stop at a value-taking short flag."""
    if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
        return False
    for char in token[1:]:
        if char in ("c", "o"):
            return True
        if char in _PYTEST_VALUE_TAKING_SHORT:
            return False
    return False


def _validate_command_token(token: str, *, allow_leading_dash: bool = True) -> None:
    if not token or token != token.strip():
        raise ValueError("Command arguments must be non-empty and contain no surrounding whitespace")
    if len(token) > _MAX_ARGUMENT_LENGTH:
        raise ValueError("Command argument is too long")
    if not allow_leading_dash and token.startswith("-"):
        raise ValueError("Command executables cannot be flags")
    if any(fragment in token for fragment in _FORBIDDEN_TOKEN_FRAGMENTS):
        raise ValueError(f"Command argument contains a forbidden shell fragment: {token!r}")
    pieces = [token]
    if "=" in token:
        pieces.append(token.split("=", 1)[1])
    for piece in pieces:
        if not piece:
            continue
        if piece.startswith(("@", "/")):
            raise ValueError(f"Command argument must stay inside the repository: {token!r}")
        if _WINDOWS_DRIVE_RE.match(piece) or "://" in piece:
            raise ValueError(f"Command argument must stay inside the repository: {token!r}")
        path = Path(piece)
        if ".." in path.parts:
            raise ValueError(f"Command argument may not traverse outside the repository: {token!r}")
        lowered_parts = {part.lower() for part in path.parts}
        if lowered_parts & _PROTECTED_COMMAND_PARTS:
            raise ValueError(f"Command argument targets a protected path: {token!r}")
        name = path.name.lower()
        if name in _BLOCKED_COMMAND_FILES or name.startswith("service-account") and name.endswith(".json"):
            raise ValueError(f"Command argument targets a protected file: {token!r}")
        if any(name.endswith(extension) for extension in _BLOCKED_COMMAND_EXTENSIONS):
            raise ValueError(f"Command argument targets a protected file: {token!r}")
        if name == ".env" or name.startswith(".env.") or name.startswith(".env_"):
            raise ValueError(f"Command argument targets a protected file: {token!r}")


class RepoCommandConfig(BaseModel):
    """Repository-scoped argv allowlist grouped by test/build/typecheck."""

    model_config = ConfigDict(extra="forbid")

    test: List[List[str]] = Field(default_factory=list)
    build: List[List[str]] = Field(default_factory=list)
    typecheck: List[List[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_commands(self):
        for category in COMMAND_CATEGORIES:
            commands = getattr(self, category)
            if len(commands) > _MAX_COMMANDS_PER_CATEGORY:
                raise ValueError(f"allowed_commands.{category} has too many entries")
            for argv in commands:
                if not argv:
                    raise ValueError(f"allowed_commands.{category} contains an empty command")
                if len(argv) > _MAX_ARGUMENTS_PER_COMMAND:
                    raise ValueError(f"allowed_commands.{category} command has too many arguments")
                executable = argv[0]
                _validate_command_token(executable, allow_leading_dash=False)
                if not _EXECUTABLE_RE.fullmatch(executable):
                    raise ValueError(f"allowed_commands.{category} requires a bare executable name")
                normalized_executable = executable.lower()
                if normalized_executable.endswith(".exe"):
                    normalized_executable = normalized_executable[:-4]
                if normalized_executable in _SHELL_EXECUTABLES:
                    raise ValueError(f"allowed_commands.{category} cannot declare a shell executable")
                if normalized_executable in _MIN_ARGUMENTS_FOR_EXECUTABLES and len(argv) < 2:
                    raise ValueError(f"allowed_commands.{category} interpreter commands need an explicit module or script")
                is_pytest = any(token.lower() == "pytest" for token in argv)
                for token in argv[1:]:
                    _validate_command_token(token)
                    raw_option = token.split("=", 1)[0]
                    option = raw_option.lower()
                    if option in _FORBIDDEN_COMMAND_OPTIONS or raw_option in _COMMON_FORBIDDEN_SHORT_OPTIONS:
                        raise ValueError(f"allowed_commands.{category} contains a workspace override option")
                    if is_pytest and (
                        option in _PYTEST_FORBIDDEN_LONG_OPTIONS
                        or raw_option in _PYTEST_FORBIDDEN_OPTIONS
                    ):
                        raise ValueError(f"allowed_commands.{category} contains a pytest config override")
                    if is_pytest and _has_combined_pytest_override(raw_option):
                        raise ValueError(f"allowed_commands.{category} contains a combined pytest config override")
        return self

    def to_mapping(self) -> dict[str, list[list[str]]]:
        return {category: [list(argv) for argv in getattr(self, category)] for category in COMMAND_CATEGORIES}


class ExecutionConfig(BaseModel):
    timeout_seconds: int = 600
    model_timeout_seconds: int = Field(default=900, ge=60, le=1800)
    git_worktree_base: str = ".worktrees"
    max_output_tokens: int = Field(default=8192, gt=0)
    max_context_tokens: int = Field(default=200000, gt=0)
    allowed_commands: Dict[str, RepoCommandConfig] = Field(
        default_factory=lambda: {
            "default": RepoCommandConfig(test=[["python", "-m", "pytest"]]),
        }
    )

    @model_validator(mode="after")
    def validate_repository_keys(self):
        for key in self.allowed_commands:
            if key == "default":
                continue
            if not _REPO_KEY_RE.fullmatch(key) or ".." in key.split("/") or key.startswith("/"):
                raise ValueError(f"allowed_commands key must be a repository-relative path: {key!r}")
        return self

    def resolve_allowed_commands(self, repo_root: Path | str | None) -> dict[str, list[list[str]]]:
        selected = None
        if repo_root:
            try:
                relative = Path(repo_root).expanduser().resolve().relative_to(PROJECT_ROOT).as_posix()
            except (OSError, ValueError):
                relative = None
            if relative:
                selected = self.allowed_commands.get(relative)
        if selected is None:
            selected = self.allowed_commands.get("default")
        if selected is None:
            return {category: [] for category in COMMAND_CATEGORIES}
        return selected.to_mapping()


class SecurityConfig(BaseModel):
    allowed_repos: List[str] = Field(default_factory=lambda: ["."])


class PolicyConfig(BaseModel):
    primary_agent: Literal["glm", "deepseek"] = "glm"
    reviewer_agent: Literal["gemini_cli", "antigravity_cli", "codex_cli"] = "antigravity_cli"
    expert_agent: str = "codex_cli"
    models: dict[str, ModelPricing] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("AGENT_COOPERATION_ENV_FILE") or str(PROJECT_ROOT / ".env"),
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
    antigravity_cli_cmd: str = "agy"
    codex_expert_model: str = "gpt-6.1-sol"
    antigravity_review_model: str = "gemini-3.8-flash-high"
    antigravity_review_effort: Literal["high"] = "high"
    gemini_review_model: str = "gemini-3.8-flash-high"
    gemini_review_effort: Literal["high"] = "high"
    gemini_cli_argv_prefix: List[str] = Field(default_factory=list)
    codex_cli_argv_prefix: List[str] = Field(default_factory=list)
    opencode_cli_argv_prefix: List[str] = Field(default_factory=list)
    antigravity_cli_argv_prefix: List[str] = Field(default_factory=list)

    # SQLite Database & Policy
    sqlite_db_path: str = Field(
        default_factory=lambda: os.getenv("AGENT_COOPERATION_DB_PATH") or str(PROJECT_ROOT / "data" / "agent_orchestrator.db")
    )
    mock_sqlite_db_path: str = Field(
        default_factory=lambda: os.getenv("AGENT_COOPERATION_MOCK_DB_PATH") or str(PROJECT_ROOT / "data" / "mock_agent_orchestrator.db")
    )
    policy_yaml_path: str = Field(
        default_factory=lambda: os.getenv("AGENT_COOPERATION_POLICY_PATH") or str(PROJECT_ROOT / "config" / "policy.yaml")
    )

    def load_policy(self) -> PolicyConfig:
        policy_path = Path(self.policy_yaml_path)
        if not policy_path.is_absolute():
            policy_path = (PROJECT_ROOT / policy_path).resolve()
        if not policy_path.exists():
            return PolicyConfig()

        with open(policy_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        agents_data = raw.get("agents", {})
        raw_models = raw.get("models", {})
        models_data = {
            k: ModelPricing(**v) for k, v in raw_models.items()
        }
        if "glm" not in models_data:
            models_data["glm"] = ModelPricing(
                model_id="glm-5.3-flashx",
                base_url="https://open.bigmodel.cn/api/paas/v4",
                input_price_per_m_usd=0.15,
                output_price_per_m_usd=0.50,
            )
        if "deepseek" not in models_data:
            models_data["deepseek"] = ModelPricing(
                model_id="deepseek-flash",
                base_url="https://api.deepseek.com/v1",
                input_price_per_m_usd=0.15,
                output_price_per_m_usd=0.60,
                peak_input_price_per_m_usd=0.30,
                peak_output_price_per_m_usd=1.20,
            )

        budget_data = BudgetConfig(**raw.get("budget", {}))
        codex_data = CodexConfig(**raw.get("codex", {}))
        repair_data = RepairConfig(**raw.get("repair", {}))
        exec_data = ExecutionConfig(**raw.get("execution", {}))
        sec_data = SecurityConfig(**raw.get("security", {}))

        return PolicyConfig(
            primary_agent=agents_data.get("primary", "glm"),
            reviewer_agent=agents_data.get("reviewer", "antigravity_cli"),
            expert_agent=agents_data.get("expert", "codex_cli"),
            models=models_data,
            budget=budget_data,
            codex=codex_data,
            repair=repair_data,
            execution=exec_data,
            security=sec_data,
        )


settings = AppSettings()
