from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
