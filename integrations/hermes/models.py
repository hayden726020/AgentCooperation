"""Data models for Hermes Agent integration with AgentCooperation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class HermesSubmitRequest(BaseModel):
    requirement: str
    repo_root: Optional[str] = None
    chosen_model: Optional[Literal["glm", "deepseek"]] = None
    title: Optional[str] = None
    mock_mode: bool = False
    custom_test_cmd: Optional[str] = None
    test_paths: Optional[List[str]] = None
    idempotency_key: Optional[str] = None


class HermesTaskResponse(BaseModel):
    task_id: str
    status: str  # SUCCESS, FAILED, WAITING_APPROVAL, WAITING_REVIEW, BUDGET_STOPPED, IN_PROGRESS, REJECTED
    state: str
    title: str
    requirement: str
    primary_model: str
    branch_name: str = ""
    worktree_path: str = ""
    total_cost_cny: float = 0.0
    repair_attempts: int = 0
    codex_calls: int = 0
    has_pending_codex_audit: bool = False
    result_summary: Optional[str] = None
    test_summary: Optional[str] = None
    review_summary: Optional[Dict[str, Any]] = None
    diff_summary: Optional[str] = None
    files_written: List[str] = Field(default_factory=list)
    deduplicated: bool = False
    error: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None


class HermesApprovalRequest(BaseModel):
    task_id: str
    approved: bool
    notes: Optional[str] = None
    mock_mode: Optional[bool] = None


class HermesApprovalResponse(BaseModel):
    task_id: str
    success: bool
    message: str
    updated_state: str


class HermesBudgetStatus(BaseModel):
    monthly_cny: float
    current_spending_cny: float
    pending_reservations_cny: float
    total_projected_cny: float
    warning_cny: float
    pause_new_cny: float
    hard_stop_cny: float
    task_limit_cny: float
    can_start_new_task: bool
    budget_message: str
    codex_quota_state: str
    codex_recovery_at: Optional[str] = None


class HermesRoleExecutionRequest(BaseModel):
    role: Literal["primary_plan", "reviewer", "expert"]
    requirement: str
    worktree_path: Optional[str] = None
    mock_mode: bool = False
    context: Optional[Dict[str, Any]] = None


class HermesRoleExecutionResponse(BaseModel):
    role: str
    success: bool
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
