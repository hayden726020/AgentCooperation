"""S7 Pilot regression test exercising execution security safeguards."""
from execution.security import contains_sensitive_files, redact_and_truncate_evidence


def test_sensitive_files_detection():
    """Verify contains_sensitive_files accurately intercepts sensitive file paths."""
    blocked, path = contains_sensitive_files(["src/main.py", ".env", "README.md"])
    assert blocked is True
    assert ".env" in path

    safe, _ = contains_sensitive_files(["src/main.py", "tests/test_app.py"])
    assert safe is False


def test_credential_redaction_and_truncation():
    """Verify redact_and_truncate_evidence strips tokens and protects output size."""
    raw = "sk-1234567890abcdef1234567890"
    redacted = redact_and_truncate_evidence(raw, max_chars=100)
    assert "sk-" not in redacted
    assert "[REDACTED_API_KEY]" in redacted
