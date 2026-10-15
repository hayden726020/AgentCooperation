"""Data models and state mappings for Multica platform integration."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

MulticaIssueStatus = Literal[
    "backlog",
    "todo",
    "in_progress",
    "in_review",
    "done",
    "blocked",
    "cancelled",
]

MulticaIssuePriority = Literal["urgent", "high", "medium", "low"]


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MulticaIssue:
    id: str
    title: str
    description: str
    status: MulticaIssueStatus = "todo"
    priority: MulticaIssuePriority = "medium"
    assignee: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    repo_path: Optional[str] = None
    branch_name: Optional[str] = None
    created_at: str = field(default_factory=utc_iso_now)
    updated_at: str = field(default_factory=utc_iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "labels": list(self.labels),
            "repo_path": self.repo_path,
            "branch_name": self.branch_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MulticaIssue":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=data.get("status", "todo"),
            priority=data.get("priority", "medium"),
            assignee=data.get("assignee"),
            labels=list(data.get("labels", [])),
            repo_path=data.get("repo_path"),
            branch_name=data.get("branch_name"),
            created_at=data.get("created_at") or utc_iso_now(),
            updated_at=data.get("updated_at") or utc_iso_now(),
        )


@dataclass
class MulticaComment:
    issue_id: str
    body: str
    author: str = "AgentCooperation"
    id: Optional[str] = None
    created_at: str = field(default_factory=utc_iso_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "issue_id": self.issue_id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MulticaComment":
        return cls(
            id=data.get("id"),
            issue_id=str(data.get("issue_id", "")),
            author=str(data.get("author", "AgentCooperation")),
            body=str(data.get("body", "")),
            created_at=data.get("created_at") or utc_iso_now(),
        )


@dataclass
class MulticaRunResult:
    issue_id: str
    task_id: str
    fsm_state: str
    multica_status: MulticaIssueStatus
    total_cost_cny: float
    branch_name: str
    summary: str
    comments_posted: List[str] = field(default_factory=list)


def map_fsm_state_to_multica_status(fsm_state: str) -> MulticaIssueStatus:
    """Map AgentCooperation finite state machine states to Multica issue states."""
    fsm_upper = fsm_state.upper()
    if fsm_upper == "SUCCESS":
        return "done"
    if fsm_upper in {"REVIEWING", "WAITING_REVIEW"}:
        return "in_review"
    if fsm_upper in {"WAITING_APPROVAL", "BUDGET_STOPPED", "CODEX_BACKLOGGED"}:
        return "blocked"
    if fsm_upper in {"FAILED", "INTERRUPTED"}:
        return "blocked"
    # Active execution phases: INIT, PLANNING, DEVELOPING, TESTING, REPAIRING, ESCALATING
    return "in_progress"
