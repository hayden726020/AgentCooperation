"""S7 Pilot regression test exercising execution security safeguards."""
import pytest
from execution.security import contains_sensitive_files, redact_and_truncate_evidence


def test_sensitive_files_detection():
    """Verify contains_sensitive_files accurately intercepts sensitive file paths."""
    blocked, path = contains_sensitive_files(["src/main.py", ".env", "README.md"])
    assert blocked is True
    assert ".env" in path

    safe, _ = contains_sensitive_files(["src/main.py", "tests/test_app.py"])
    assert safe is False


@pytest.mark.parametrize(
    "secret,template",
    [
        ("sk-1234567890abcdef1234567890", "openai_key = {s}"),
        ("ghp_1234567890abcdef1234567890abcdef123456", "github_token = {s}"),
        ("AKIA1234567890ABCDEF", "aws_access_key = {s}"),
    ],
)
def test_credential_redaction_patterns(secret, template):
    """Verify redact_and_truncate_evidence strips diverse secret shapes while preserving context."""
    raw = template.format(s=secret)
    redacted = redact_and_truncate_evidence(raw, max_chars=1000)
    assert secret not in redacted
    prefix = template.split("{s}")[0]
    assert prefix in redacted


def test_evidence_truncation_when_exceeding_limit():
    """Verify evidence exceeding max_chars is trimmed and marked to prevent token blowout."""
    long_content = "important execution log line\n" * 40
    limit = 80
    truncated = redact_and_truncate_evidence(long_content, max_chars=limit)
    assert len(truncated) < len(long_content)
    assert "[TRUNCATED" in truncated
