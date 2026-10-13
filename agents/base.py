import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional


@dataclass
class AgentResult:
    success: bool
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    duration_s: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseAgent(ABC):
    def __init__(self, role_name: str, model_name: str):
        self.role_name = role_name
        self.model_name = model_name

    @abstractmethod
    async def execute(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """执行该 Agent 的特定职责"""
        pass


def task_directory(context: dict[str, Any]) -> Path:
    """Require the caller to bind a CLI operation to an existing task checkout."""
    value = context.get("worktree_path")
    if not value:
        raise ValueError("worktree_path is required for a task-scoped CLI call")
    path = Path(value).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("worktree_path must be a directory")
    return path


def redact_credentials(text: str, credential_env: dict[str, str]) -> str:
    """Remove explicitly passed credential values from model diagnostics."""
    for credential in credential_env.values():
        if credential:
            text = text.replace(credential, "[redacted]")
    return text


def parse_review_output(raw_text: str) -> dict[str, Any]:
    """Unwrap CLI envelopes and validate every field needed for a review decision."""
    value: Any = raw_text
    for _ in range(4):
        if isinstance(value, str):
            text = value.strip()
            fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            value = json.loads(fenced.group(1) if fenced else text)
        if not isinstance(value, dict):
            raise ValueError("Review output must be a JSON object")
        if value.get("error"):
            raise ValueError("Review CLI reported an error")
        if "approved" in value:
            break
        if "response" in value:
            value = value["response"]
        elif "review" in value:
            value = value["review"]
        else:
            raise ValueError("Review output has no review decision")
    if not isinstance(value, dict) or type(value.get("approved")) is not bool:
        raise ValueError("Review approved must be a boolean")
    if value.get("severity") not in {"P0", "P1", "P2", "P3"}:
        raise ValueError("Review severity must be P0, P1, P2 or P3")
    if not isinstance(value.get("summary"), str):
        raise ValueError("Review summary must be a string")
    for field_name in ("critical_issues", "suggestions"):
        items = value.get(field_name)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError(f"Review {field_name} must be a list of strings")
    return value
