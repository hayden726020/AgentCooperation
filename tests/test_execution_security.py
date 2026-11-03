import time

import pytest

from execution.security import contains_sensitive_files, redact_and_truncate_evidence


def test_contains_sensitive_files_safe():
    safe_files = ["orchestrator/core.py", "tests/test_core.py", "README.md", "config/policy.yaml"]
    has_sens, path = contains_sensitive_files(safe_files)
    assert has_sens is False
    assert path == ""


def test_contains_sensitive_files_blocked():
    sensitive_cases = [
        [".env"],
        ["config/.env.local"],
        [".git/HEAD"],
        ["ssh/id_rsa"],
        ["certs/server.pem"],
        ["keys/secret.key"],
        ["id_ed25519"],
    ]
    for case in sensitive_cases:
        has_sens, path = contains_sensitive_files(case)
        assert has_sens is True, f"Failed to detect sensitive file in {case}"
        assert path == case[0]


def test_redact_and_truncate_evidence_secrets():
    raw = (
        "api_key = 'sk-1234567890abcdef123456'\n"
        "DATABASE_URL = postgres://user:password123@localhost:5432/mydb\n"
        "AWS: AKIA1234567890ABCDEF\n"
        "GITHUB: ghp_123456789012345678901234567890123456\n"
    )
    redacted = redact_and_truncate_evidence(raw)
    assert "sk-1234567890abcdef123456" not in redacted
    assert "password123" not in redacted
    assert "AKIA1234567890ABCDEF" not in redacted
    assert "ghp_123456789012345678901234567890123456" not in redacted
    assert "[REDACTED" in redacted


def test_redact_and_truncate_evidence_json_style_keys():
    raw = (
        '{"api_key": "abcdefgh12345678", "client_secret": "zyxwvuts87654321"}\n'
        '{"X-Api-Key": "HHHHHHHH12345678", "X-Auth-Token": "TTTTTTTT12345678"}\n'
        '{"MY_TOKEN": "MMMMMMMM12345678"}\n'
        'secret_key = "qwertyuiop12345678"\n'
        'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart\n'
        'Authorization: Basic dXNlcjpwYXNzd29yZA==\n'
        '{"Authorization": "Basic cXVvdGVkOnBhc3N3b3Jk"}\n'
        'headers = {"Authorization": "Bearer abcdEFGH12345678"}\n'
    )
    redacted = redact_and_truncate_evidence(raw)
    assert "abcdefgh12345678" not in redacted
    assert "zyxwvuts87654321" not in redacted
    assert "HHHHHHHH12345678" not in redacted
    assert "TTTTTTTT12345678" not in redacted
    assert "MMMMMMMM12345678" not in redacted
    assert "qwertyuiop12345678" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "dXNlcjpwYXNzd29yZA" not in redacted
    assert "cXVvdGVkOnBhc3N3b3Jk" not in redacted
    assert "abcdEFGH12345678" not in redacted
    assert "[REDACTED" in redacted


def test_redaction_scales_linearly_on_long_alphanumeric_text():
    started = time.monotonic()
    redact_and_truncate_evidence("A" * 20000)
    assert time.monotonic() - started < 2.0


def test_redact_and_truncate_evidence_length_limit():
    long_content = "A" * 1000
    truncated = redact_and_truncate_evidence(long_content, max_chars=100)
    assert len(truncated) > 100
    assert "[TRUNCATED 900 characters" in truncated
    assert truncated.startswith("A" * 100)
