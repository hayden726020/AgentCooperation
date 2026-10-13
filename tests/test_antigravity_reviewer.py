"""No model service calls: recorded Antigravity NDJSON transport contracts."""

import importlib
import json

import pytest


def adapter():
    return importlib.import_module("agents.antigravity_reviewer")


def verdict():
    return {"approved": True, "severity": "P3", "summary": "fixture",
            "critical_issues": [], "suggestions": []}


def output(result):
    return json.dumps({"event": "init"}) + "\n" + json.dumps({"event": "result", "result": result}) + "\n"


def test_structured_success_envelope_is_validated():
    review = adapter().parse_antigravity_review(output({
        "status": "SUCCESS", "response": "ok", "structured_output": verdict(),
    }))
    assert review == verdict()


@pytest.mark.parametrize("raw", [
    "", "not JSON\n", output({"status": "FAILED", "structured_output": verdict()}),
    output({"status": "SUCCESS", "response": json.dumps(verdict())}),
    output({"status": "SUCCESS", "structured_output": {**verdict(), "approved": "true"}}),
    output({"status": "SUCCESS", "structured_output": verdict()}) * 2,
])
def test_incomplete_or_ambiguous_result_cannot_approve(raw):
    with pytest.raises(ValueError):
        adapter().parse_antigravity_review(raw)
