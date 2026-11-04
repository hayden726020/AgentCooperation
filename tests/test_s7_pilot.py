"""S7 Pilot regression test exercising execution security safeguards."""
from execution.security import contains_sensitive_files, redact_and_truncate_evidence


def test_sensitive_files_detection():
    """Verify contains_sensitive_files accurately intercepts sensitive file paths."""
    blocked, path = contains_sensitive_files(["src/main.py", ".env", "README.md"])
    assert blocked is True
    assert ".env" in path

    safe, _ = contains_sensitive_files(["src/main.py", "tests/test_app.py"])
    assert safe is False


def test_credential_redaction():
    """Verify redact_and_truncate_evidence removes sensitive API keys from text."""
    secret = "sk-1234567890abcdef1234567890"
    raw = f"api_token = {secret}"
    redacted = redact_and_truncate_evidence(raw, max_chars=1000)
    assert secret not in redacted
    assert redacted != raw


def test_evidence_truncation_when_exceeding_limit():
    """Verify evidence exceeding max_chars is truncated to prevent token explosion."""
    long_content = "log line data " * 50
    limit = 60
    truncated = redact_and_truncate_evidence(long_content, max_chars=limit)
    assert "[TRUNCATED" in truncated
    assert truncated.startswith(long_content[:limit])
