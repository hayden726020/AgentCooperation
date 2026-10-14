"""Regression tests for review output parsing across Gemini, Codex, and Antigravity."""

import json
from unittest.mock import AsyncMock

import pytest

from agents.antigravity_reviewer import AntigravityReviewer
from agents.base import parse_review_output
from agents.codex_expert import CodexExpert
from agents.gemini_reviewer import GeminiReviewer
from execution.subproc import SubprocResult


def valid_review(**kwargs):
    payload = {
        "approved": True,
        "severity": "P3",
        "summary": "Implementation is verified and clean.",
        "critical_issues": [],
        "suggestions": [],
    }
    payload.update(kwargs)
    return payload


# 1. Pure JSON
def test_valid_pure_json():
    review = valid_review()
    parsed = parse_review_output(json.dumps(review))
    assert parsed == review


# 2. Single fenced JSON (with ```json and ```)
def test_valid_single_fenced_json():
    review = valid_review()
    text = f"```json\n{json.dumps(review)}\n```"
    parsed = parse_review_output(text)
    assert parsed == review


def test_valid_single_fenced_without_language():
    review = valid_review()
    text = f"```\n{json.dumps(review)}\n```"
    parsed = parse_review_output(text)
    assert parsed == review


# 3. Identical duplicate objects accepted
def test_identical_duplicate_fenced_objects_accepted():
    review = valid_review()
    text = f"```json\n{json.dumps(review)}\n```\n```json\n{json.dumps(review)}\n```"
    parsed = parse_review_output(text)
    assert parsed == review


def test_identical_duplicate_pure_json_objects_accepted():
    review = valid_review()
    text = f"{json.dumps(review)}\n{json.dumps(review)}"
    parsed = parse_review_output(text)
    assert parsed == review


# 4. Conflicting objects must be rejected
def test_conflicting_approved_then_rejected_rejected():
    r1 = valid_review(approved=True, severity="P3", summary="looks good")
    r2 = valid_review(approved=False, severity="P1", summary="broken", critical_issues=["critical bug"])
    text = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    with pytest.raises(ValueError, match="(?i)conflict|ambiguous|mismatch|multiple"):
        parse_review_output(text)


def test_conflicting_rejected_then_approved_rejected():
    r1 = valid_review(approved=False, severity="P1", summary="broken", critical_issues=["critical bug"])
    r2 = valid_review(approved=True, severity="P3", summary="looks good")
    text = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    with pytest.raises(ValueError, match="(?i)conflict|ambiguous|mismatch|multiple"):
        parse_review_output(text)


def test_conflicting_same_approved_different_summary_or_suggestions_rejected():
    r1 = valid_review(summary="summary 1")
    r2 = valid_review(summary="summary 2")
    text = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    with pytest.raises(ValueError, match="(?i)conflict|ambiguous|mismatch|multiple"):
        parse_review_output(text)

    r3 = valid_review(suggestions=["suggestion A"])
    r4 = valid_review(suggestions=["suggestion B"])
    text_sugg = f"{json.dumps(r3)}\n{json.dumps(r4)}"
    with pytest.raises(ValueError, match="(?i)conflict|ambiguous|mismatch|multiple"):
        parse_review_output(text_sugg)


# 5. Trailing error object must be rejected
def test_trailing_error_object_rejected():
    review = valid_review()
    text = f"{json.dumps(review)}\n{{\"error\": \"review failed\"}}"
    with pytest.raises(ValueError, match="(?i)error|unexpected|invalid|trailing"):
        parse_review_output(text)


def test_trailing_error_object_after_fence_rejected():
    review = valid_review()
    text = f"```json\n{json.dumps(review)}\n```\n{{\"error\": \"review failed\"}}"
    with pytest.raises(ValueError, match="(?i)error|unexpected|invalid|trailing"):
        parse_review_output(text)


# 6. Trailing second non-review JSON object must be rejected
def test_trailing_non_review_json_object_rejected():
    review = valid_review()
    text = f"{json.dumps(review)}\n{{\"extra_data\": 123}}"
    with pytest.raises(ValueError, match="(?i)unexpected|invalid|trailing|multiple"):
        parse_review_output(text)


def test_trailing_non_review_inside_fence_rejected():
    review = valid_review()
    text = f"```json\n{json.dumps(review)}\n{{\"extra_data\": 123}}\n```"
    with pytest.raises(ValueError, match="(?i)unexpected|invalid|trailing|multiple"):
        parse_review_output(text)


# 7. Duplicate JSON keys rejected
def test_duplicate_json_keys_rejected():
    raw_json = '{"approved": false, "approved": true, "severity": "P3", "summary": "ok", "critical_issues": [], "suggestions": []}'
    with pytest.raises(ValueError, match="(?i)duplicate"):
        parse_review_output(raw_json)


# 8. Missing required fields rejected
@pytest.mark.parametrize("missing_field", ["approved", "severity", "summary", "critical_issues", "suggestions"])
def test_missing_required_fields_rejected(missing_field):
    review = valid_review()
    del review[missing_field]
    with pytest.raises(ValueError, match="(?i)missing|review|boolean|string|severity"):
        parse_review_output(json.dumps(review))


# 9. Wrong field types rejected
@pytest.mark.parametrize("field,bad_value", [
    ("approved", "true"),
    ("approved", None),
    ("severity", "INVALID"),
    ("severity", 0),
    ("summary", 123),
    ("summary", None),
    ("critical_issues", "not a list"),
    ("critical_issues", [123]),
    ("suggestions", "not a list"),
    ("suggestions", [True]),
])
def test_wrong_field_types_rejected(field, bad_value):
    review = valid_review(**{field: bad_value})
    with pytest.raises(ValueError):
        parse_review_output(json.dumps(review))


# 10. Truncated JSON rejected
def test_truncated_json_rejected():
    truncated = '{"approved": true, "severity": "P3", "summary": "cut off'
    with pytest.raises(ValueError):
        parse_review_output(truncated)

    truncated_fence = '```json\n{"approved": true, "severity": "P3", "summary": "cut off'
    with pytest.raises(ValueError):
        parse_review_output(truncated_fence)


# 11. Abnormal trailing content rejected
def test_abnormal_trailing_content_rejected():
    review = valid_review()
    text = f"{json.dumps(review)} some extra unexpected text"
    with pytest.raises(ValueError, match="(?i)unexpected|invalid|trailing|extra|json"):
        parse_review_output(text)

    text_fence = f"```json\n{json.dumps(review)}\n``` some extra unexpected text"
    with pytest.raises(ValueError, match="(?i)unexpected|invalid|trailing|extra|json"):
        parse_review_output(text_fence)


# 12. Shared parser integration paths: Gemini, Codex, Antigravity
@pytest.mark.asyncio
async def test_gemini_reviewer_fails_on_conflicting_reviews(monkeypatch, tmp_path):
    r1 = valid_review(approved=True, severity="P3", summary="looks good")
    r2 = valid_review(approved=False, severity="P1", summary="bad", critical_issues=["issue"])
    payload = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    command = AsyncMock(return_value=SubprocResult(["gemini"], 0, json.dumps({"response": payload}), "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    result = await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert result.success is False
    assert not result.metadata.get("review", {}).get("approved", False)


@pytest.mark.asyncio
async def test_gemini_reviewer_fails_on_trailing_error(monkeypatch, tmp_path):
    review = valid_review()
    payload = f"{json.dumps(review)}\n{{\"error\": \"internal error\"}}"
    command = AsyncMock(return_value=SubprocResult(["gemini"], 0, payload, "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    result = await GeminiReviewer().execute("fixture", {"worktree_path": tmp_path})
    assert result.success is False
    assert not result.metadata.get("review", {}).get("approved", False)


@pytest.mark.asyncio
async def test_codex_expert_fails_on_conflicting_reviews(monkeypatch, tmp_path):
    r1 = valid_review(approved=True, severity="P3", summary="looks good")
    r2 = valid_review(approved=False, severity="P1", summary="bad", critical_issues=["issue"])
    payload = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    command = AsyncMock(return_value=SubprocResult(["codex"], 0, payload, "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    monkeypatch.setattr(CodexExpert, "_verify_expected_head", AsyncMock())
    result = await CodexExpert().execute("fixture", {
        "worktree_path": tmp_path, "expected_commit_sha": "a" * 40, "structured_review": True,
    })
    assert result.success is False or "review" not in result.metadata or not result.metadata["review"].get("approved")


@pytest.mark.asyncio
async def test_antigravity_reviewer_fails_on_conflicting_or_trailing_error(monkeypatch, tmp_path):
    r1 = valid_review(approved=True, severity="P3", summary="looks good")
    r2 = valid_review(approved=False, severity="P1", summary="bad", critical_issues=["issue"])
    payload = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"

    async def fake_command(cmd, cwd=None, env=None, timeout=None, input_text=None):
        stream = (
            json.dumps({"event": "init", "init": {"agent": "orchestrator-readonly-reviewer", "tools": [], "model": "gemini-3.8-flash-low", "cwd": str(cwd)}}) + "\n"
            + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": payload}}) + "\n"
        )
        return SubprocResult(["agy"], 0, stream, "", 0)

    monkeypatch.setattr("agents.antigravity_reviewer.run_async_command", fake_command)
    monkeypatch.setattr(CodexExpert, "_verify_expected_head", AsyncMock())
    result = await AntigravityReviewer().execute("fixture", {
        "worktree_path": tmp_path, "expected_commit_sha": "a" * 40,
    })
    assert result.success is False
    assert "conflicting" in result.error.lower() or "multiple" in result.error.lower()


# 13. Candidate boolean vs numeric equality strict distinction
@pytest.mark.parametrize("cand1_approved,cand2_approved", [
    (True, 1),
    (1, True),
    (True, 1.0),
    (1.0, True),
    (False, 0),
    (0, False),
    (False, 0.0),
    (0.0, False),
])
def test_candidate_boolean_numeric_strict_distinction_rejected(cand1_approved, cand2_approved):
    r1 = valid_review()
    r1["approved"] = cand1_approved
    r2 = valid_review()
    r2["approved"] = cand2_approved
    text = f"```json\n{json.dumps(r1)}\n```\n```json\n{json.dumps(r2)}\n```"
    with pytest.raises(ValueError):
        parse_review_output(text)


# 14. Valid JSON with Markdown triple backticks inside strings
def test_pure_json_with_code_blocks_in_summary_accepted():
    summary_with_code = "Use the following snippet:\n```python\nprint('hello')\n```\nand run pytest."
    review = valid_review(summary=summary_with_code)
    text = json.dumps(review)
    parsed = parse_review_output(text)
    assert parsed == review


def test_fenced_json_with_code_blocks_in_summary_accepted():
    summary_with_code = "Use the following snippet:\n```python\nprint('hello')\n```\nand run pytest."
    review = valid_review(summary=summary_with_code)
    text = f"```json\n{json.dumps(review)}\n```"
    parsed = parse_review_output(text)
    assert parsed == review


def test_json_with_odd_backticks_in_string_accepted():
    summary_with_odd_backticks = "Check out ``` for formatting code."
    review = valid_review(summary=summary_with_odd_backticks)
    text = json.dumps(review)
    parsed = parse_review_output(text)
    assert parsed == review


def test_fenced_json_with_code_blocks_in_suggestions_accepted():
    sugg = ["Query using ```sql\nSELECT * FROM users;\n``` to check records."]
    review = valid_review(suggestions=sugg)
    text = f"```json\n{json.dumps(review)}\n```"
    parsed = parse_review_output(text)
    assert parsed == review


# 15. Antigravity event stream duplicate keys rejection
def test_antigravity_event_stream_duplicate_status_key_rejected():
    from agents.antigravity_reviewer import parse_antigravity_review
    stream = (
        '{"event": "init", "init": {"agent": "orchestrator-readonly-reviewer", "tools": [], "model": "gemini-3.8-flash-low"}}\n'
        '{"event": "result", "result": {"status": "ERROR", "status": "SUCCESS", "structured_output": {"approved": true, "severity": "P3", "summary": "ok", "critical_issues": [], "suggestions": []}}}\n'
    )
    with pytest.raises(ValueError, match="(?i)duplicate"):
        parse_antigravity_review(stream)


def test_antigravity_event_stream_duplicate_approved_key_in_structured_output_rejected():
    from agents.antigravity_reviewer import parse_antigravity_review
    stream = (
        '{"event": "init", "init": {"agent": "orchestrator-readonly-reviewer", "tools": [], "model": "gemini-3.8-flash-low"}}\n'
        '{"event": "result", "result": {"status": "SUCCESS", "structured_output": {"approved": false, "approved": true, "severity": "P3", "summary": "ok", "critical_issues": [], "suggestions": []}}}\n'
    )
    with pytest.raises(ValueError, match="(?i)duplicate"):
        parse_antigravity_review(stream)

