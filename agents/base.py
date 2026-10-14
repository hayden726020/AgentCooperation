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


def redact_review_credentials(review: dict[str, Any], credential_env: dict[str, str]) -> dict[str, Any]:
    """Sanitize validated, decoded review prose without changing decision types."""
    from execution.proxy import redact_cli_proxy_credentials

    api_credentials = {key: value for key, value in credential_env.items()
                       if key.upper().endswith(("_API_KEY", "_TOKEN"))}

    def safe_text(value: str) -> str:
        return redact_cli_proxy_credentials(redact_credentials(value, api_credentials), credential_env)

    return {
        "approved": review["approved"], "severity": review["severity"],
        "summary": safe_text(review["summary"]),
        "critical_issues": [safe_text(value) for value in review["critical_issues"]],
        "suggestions": [safe_text(value) for value in review["suggestions"]],
    }


def _unique_json_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for key, val in pairs:
        if key in res:
            raise ValueError(f"Duplicate JSON key: {key}")
        res[key] = val
    return res


_STRICT_JSON_DECODER = json.JSONDecoder(object_pairs_hook=_unique_json_pairs_hook)
_FENCE_REGEX = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\r?\n?([\s\S]*?)```")


def _parse_json_stream(s: str) -> list[Any]:
    """Parse one or more contiguous JSON values from string `s`.

    Requires all non-whitespace characters to be consumed by valid JSON values.
    Raises ValueError on syntax error, duplicate keys, truncated JSON, or unconsumed content.
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty JSON content")
    idx = 0
    length = len(s)
    objects: list[Any] = []
    while idx < length:
        while idx < length and s[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            obj, end_idx = _STRICT_JSON_DECODER.raw_decode(s, idx)
        except Exception as exc:
            raise ValueError(f"Malformed or truncated JSON: {exc}") from exc
        objects.append(obj)
        idx = end_idx
        if idx < length and not s[idx].isspace() and s[idx] not in ("{", "["):
            raise ValueError(f"Abnormal unconsumed trailing content: {s[idx:idx+30]!r}")
    return objects


def _extract_candidate_objects(text: str) -> list[Any]:
    """Extract and strictly validate all candidate JSON objects from plain or fenced text."""
    text = text.strip()
    if not text:
        raise ValueError("Review output is empty")

    fence_count = text.count("```")
    if fence_count > 0:
        if fence_count % 2 != 0:
            raise ValueError("Unclosed or malformed Markdown code fence in review output")
        matches = list(_FENCE_REGEX.finditer(text))
        if len(matches) * 2 != fence_count:
            raise ValueError("Malformed or unmatched Markdown code fences in review output")

        pre_text = text[:matches[0].start()].strip()
        if pre_text:
            if any(c in pre_text for c in "{}[]"):
                raise ValueError("Review peripheral text contains un-fenced JSON delimiters")
            if re.search(r"\b(error|failed|exception)\b", pre_text, re.IGNORECASE):
                raise ValueError(f"Review preamble reports an error: {pre_text[:100]!r}")
            if len(pre_text) > 300:
                raise ValueError("Unexpectedly large preamble before review fence")

        for i in range(len(matches) - 1):
            mid_text = text[matches[i].end():matches[i + 1].start()].strip()
            if mid_text:
                raise ValueError(f"Abnormal unconsumed content between review fences: {mid_text[:100]!r}")

        post_text = text[matches[-1].end():].strip()
        if post_text:
            raise ValueError(f"Abnormal trailing content after review fence: {post_text[:100]!r}")

        all_objects: list[Any] = []
        for m in matches:
            body = m.group(1).strip()
            if not body:
                raise ValueError("Empty review code fence in review output")
            all_objects.extend(_parse_json_stream(body))
        return all_objects
    else:
        return _parse_json_stream(text)


def _validate_review_verdict(value: dict[str, Any]) -> dict[str, Any]:
    """Validate all required fields and types for a finalized review decision."""
    if not isinstance(value, dict):
        raise ValueError("Review output must be a JSON object")
    if value.get("error"):
        raise ValueError(f"Review CLI reported an error: {value.get('error')}")
    if type(value.get("approved")) is not bool:
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


def parse_review_output(raw_text: str) -> dict[str, Any]:
    """Unwrap CLI envelopes and validate every field needed for a review decision."""
    value: Any = raw_text
    for _ in range(4):
        if isinstance(value, str):
            all_objects = _extract_candidate_objects(value)
            if not all_objects:
                raise ValueError("Review output contains no review objects")
            for obj in all_objects:
                if isinstance(obj, dict) and obj.get("error"):
                    raise ValueError(f"Review CLI reported a trailing error object: {obj.get('error')}")
            if any(not isinstance(obj, dict) for obj in all_objects):
                raise ValueError("Encountered unexpected non-review JSON entity in review output")
            first_obj = all_objects[0]
            if any(obj != first_obj for obj in all_objects):
                raise ValueError("Multiple conflicting review decisions, trailing objects, or inconsistent entities in output")
            value = first_obj

        if not isinstance(value, dict):
            raise ValueError("Review output must be a JSON object")
        if value.get("error"):
            raise ValueError(f"Review CLI reported an error: {value.get('error')}")
        if "approved" in value:
            break
        if "response" in value:
            value = value["response"]
        elif "review" in value:
            value = value["review"]
        else:
            raise ValueError("Review output has no review decision")

    return _validate_review_verdict(value)

