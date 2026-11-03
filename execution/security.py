"""Security, credential redaction, and sensitive path safeguards for execution pipelines."""

import re
from typing import Sequence

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(api[_-]?key|secret|token|password|passwd|credential|authorization|"
    r"bearer|private[_-]?key)(?:$|[_-])"
)

_KEY_VALUE_RE = re.compile(
    r"""(?ix)
    (?P<keyquote>["']?)
    (?P<key>[A-Za-z0-9_.-]{1,64})
    (?P=keyquote)
    (?P<sep>\s*[:=]\s*)
    (?P<valquote>["']?)
    (?P<value>[^\s"',}]{8,})
    (?P=valquote)
    """
)


def _redact_key_value(match: re.Match) -> str:
    key = match.group("key")
    if not _SENSITIVE_KEY_RE.search(key):
        return match.group(0)
    quote = match.group("keyquote")
    value_quote = match.group("valquote")
    return f"{quote}{key}{quote}{match.group('sep')}{value_quote}[REDACTED]{value_quote}"


_SENSITIVE_PATTERNS = [
    (_KEY_VALUE_RE, _redact_key_value),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED_GOOGLE_API_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82}"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)(postgres|postgresql|mysql|mongodb|redis):\/\/[^\s:]+:[^\s@]+@[^\s\/]+"), r"\1://[REDACTED_DB_CREDENTIALS]"),
    (re.compile(r"Bearer\s+ya29\.[A-Za-z0-9_\-]+", re.IGNORECASE), "Bearer [REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"ya29\.[A-Za-z0-9_\-]+"), "[REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"""(?ix)\bauthorization["']?\s*[:=]\s*["']?basic\s+[A-Za-z0-9+/=]{8,}["']?"""), "authorization=[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.+/=]{8,}"), "Bearer [REDACTED_TOKEN]"),
]

_SENSITIVE_FILES_PATTERN = re.compile(
    r"(^|[\/\\])(\.env(\..+)?|\.git|\.aws|\.codex|id_rsa|id_ed25519|.*\.pem|.*\.key)$",
    re.IGNORECASE,
)


def contains_sensitive_files(paths: Sequence[str]) -> tuple[bool, str]:
    """Check if any path matches sensitive patterns (.env, .git, .aws, keys, certs)."""
    for p in paths:
        p_clean = str(p).replace("\\", "/").strip("/")
        parts = p_clean.split("/")
        for part in parts:
            if _SENSITIVE_FILES_PATTERN.match(part):
                return True, str(p)
        if _SENSITIVE_FILES_PATTERN.match(p_clean):
            return True, str(p)
    return False, ""


def redact_and_truncate_evidence(content: str, max_chars: int = 40000) -> str:
    """Redact known credential patterns and truncate evidence to prevent token blowout."""
    if not content:
        return ""
    text = content
    for pattern, repl in _SENSITIVE_PATTERNS:
        text = pattern.sub(repl, text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [TRUNCATED {len(text) - max_chars} characters for review safety]"
    return text
