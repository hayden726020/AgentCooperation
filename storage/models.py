from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)
    title: str
    requirement: str
    primary_model: str = "glm"  # "glm" 或 "deepseek" (互斥单模型)
    state: str = "PENDING"
    branch_name: str = ""
    worktree_path: str = ""
    repair_attempts: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_cny: float = 0.0
    codex_calls: int = 0
    has_pending_codex_audit: bool = False
    result_summary: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None


class ExecutionLog(SQLModel, table=True):
    __tablename__ = "execution_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    step_name: str
    agent_role: str  # "PRIMARY", "REVIEWER", "EXPERT", "TEST_RUNNER", "SYSTEM"
    agent_model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    status: str = "SUCCESS"  # "SUCCESS", "FAILED", "TIMEOUT", "SKIPPED"
    output_snippet: Optional[str] = None
    error_snippet: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class PendingCodexAudit(SQLModel, table=True):
    """
    当 Codex 周额度耗尽 (EXHAUSTED) 时，暂存重要问题，
    允许当前任务优雅降级完成，待额度恢复后由后台补审。
    """
    __tablename__ = "pending_codex_audits"

    id: Optional[int] = Field(default=None, primary_key=True)
    audit_id: str = Field(index=True, unique=True)
    task_id: str = Field(index=True)
    commit_sha: str = ""
    severity: str = "P1"  # "P0", "P1"
    issue_description: str
    git_diff_summary: str
    gemini_review_json: Optional[str] = None
    status: str = "QUEUED"  # "QUEUED", "AUDITING", "RESOLVED", "CLOSED"
    codex_feedback: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None


class CodexQuotaRecord(SQLModel, table=True):
    """记录 Codex 周额度状态机状态与检测记录"""
    __tablename__ = "codex_quota_records"

    id: Optional[int] = Field(default=None, primary_key=True)
    state: str = "READY"  # READY, RESERVED, EXHAUSTED, RECOVERING
    last_checked_at: datetime = Field(default_factory=utc_now)
    exhausted_reason: Optional[str] = None
    estimated_recovery_at: Optional[datetime] = None


class MonthlySpending(SQLModel, table=True):
    """按月份记录 API 费用支出，守护 ¥200/月 预算"""
    __tablename__ = "monthly_spending"

    id: Optional[int] = Field(default=None, primary_key=True)
    year_month: str = Field(index=True, unique=True)  # 如 "2026-10"
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_cny: float = 0.0
    updated_at: datetime = Field(default_factory=utc_now)
