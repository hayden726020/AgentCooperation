"""No model service calls: recorded Antigravity NDJSON transport contracts."""

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from execution.subproc import SubprocResult


def adapter():
    return importlib.import_module("agents.antigravity_reviewer")


@pytest.fixture(autouse=True)
def no_machine_proxy(monkeypatch):
    monkeypatch.setattr(adapter(), "get_cli_proxy_env", lambda: {})


def verdict():
    return {"approved": True, "severity": "P3", "summary": "fixture",
            "critical_issues": [], "suggestions": []}


def output(result, model="gemini-3.8-flash-high", cwd=None, tools=None):
    initialization = {"agent": "orchestrator-readonly-reviewer", "tools": tools or [], "model": model}
    if cwd is not None:
        initialization["cwd"] = str(cwd)
    return json.dumps({"event": "init", "init": initialization}) + "\n" + json.dumps({"event": "result", "result": result}) + "\n"


def test_structured_success_envelope_is_validated():
    review = adapter().parse_antigravity_review(output({
        "status": "SUCCESS", "response": "ok", "structured_output": verdict(),
    }))
    assert review == verdict()


def test_response_fallback_review_envelope_is_validated():
    review = adapter().parse_antigravity_review(output({
        "status": "SUCCESS", "response": json.dumps(verdict()),
    }))
    assert review == verdict()


def test_response_fenced_json_is_validated():
    review = adapter().parse_antigravity_review(output({
        "status": "SUCCESS", "response": f"```json\n{json.dumps(verdict())}\n```",
    }))
    assert review == verdict()


@pytest.mark.parametrize("raw", [
    "", "not JSON\n", output({"status": "FAILED", "structured_output": verdict()}),
    output({"status": "FAILED", "response": json.dumps(verdict())}),
    output({"status": "SUCCESS"}),
    output({"status": "SUCCESS", "response": ""}),
    output({"status": "SUCCESS", "response": "not valid json"}),
    output({"status": "SUCCESS", "response": json.dumps({**verdict(), "approved": "true"})}),
    output({"status": "SUCCESS", "response": json.dumps({**verdict(), "severity": "INVALID"})}),
    output({"status": "SUCCESS", "response": json.dumps({**verdict(), "summary": 123})}),
    output({"status": "SUCCESS", "response": json.dumps({**verdict(), "critical_issues": "not_a_list"})}),
    output({"status": "SUCCESS", "response": json.dumps({**verdict(), "suggestions": [123]})}),
    output({"status": "SUCCESS", "structured_output": {**verdict(), "approved": "true"}}),
    output({"status": "SUCCESS", "structured_output": verdict()}) * 2,
])
def test_incomplete_or_ambiguous_result_cannot_approve(raw):
    with pytest.raises(ValueError):
        adapter().parse_antigravity_review(raw)


@pytest.mark.parametrize("init", [{"tools": ["write_to_file"]}, {"tools": []},
                                   {"agent": "orchestrator-readonly-reviewer"}])
def test_missing_or_unsafe_agent_initialization_cannot_approve(init):
    raw = output({"status": "SUCCESS", "structured_output": verdict()}).splitlines()
    raw[0] = json.dumps({"event": "init", "init": init})
    with pytest.raises(ValueError):
        adapter().parse_antigravity_review("\n".join(raw))


@pytest.mark.parametrize("extra", [
    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "write_to_file"}},
    {"event": "step_update", "step_update": {"step_type": "agent_response", "subagent_info": {"subagents": []}}},
    {"event": "unknown"},
])
def test_tool_or_unknown_steps_cannot_approve(extra):
    raw = output({"status": "SUCCESS", "structured_output": verdict()}).splitlines()
    raw.insert(1, json.dumps(extra))
    with pytest.raises(ValueError):
        adapter().parse_antigravity_review("\n".join(raw))


@pytest.mark.asyncio
async def test_transport_failure_cannot_approve(monkeypatch, tmp_path):
    module = adapter()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", AsyncMock())
    monkeypatch.setattr(module, "run_async_command", AsyncMock(return_value=SubprocResult(
        [], 1, "", "location unsupported", 0,
    )))
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40},
    )
    assert not result.success and "location unsupported" in result.error


@pytest.mark.asyncio
async def test_explicit_mock_is_identified():
    result = await adapter().AntigravityReviewer(mock_mode=True).execute("Review")
    assert result.success and result.metadata["mock"] is True


@pytest.mark.asyncio
async def test_review_uses_isolated_tool_free_agent_and_ndjson_stdin(monkeypatch, tmp_path):
    module = adapter()
    verification = AsyncMock()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", verification)
    requirement = "中文 & | %PATH%\n" * 3000

    async def cli(command, **kwargs):
        root = kwargs["cwd"]
        assert root != tmp_path
        agent = root / ".agents/agents/orchestrator-readonly-reviewer.md"
        profile = yaml.safe_load(agent.read_text(encoding="utf-8").split("---")[1])
        assert profile["tools"] == []
        assert profile["mcpServers"] == []
        assert profile["skills"] == [] and profile["plugins"] == []
        assert profile["commandExecutionPolicy"] == "off"
        assert profile["mainAgent"] is True and profile["subagent"] is False
        message = json.loads(kwargs["input_text"])
        assert message["event"] == "user"
        assert requirement in message["message"]["content"]
        assert requirement not in " ".join(command)
        assert "-p" not in command and "--print" not in command
        assert command[command.index("--agent") + 1] == profile["name"]
        assert "--disable-slash-commands" in command
        assert "--dangerously-skip-permissions" not in command
        environment = kwargs["env"]
        assert environment["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        assert environment["HOME"] == environment["USERPROFILE"]
        home = Path(environment["HOME"])
        assert home != root and home != tmp_path
        policy = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text(encoding="utf-8"))
        assert set(policy) == {"permissions"}
        assert policy["permissions"]["allow"] == [] and policy["permissions"]["ask"] == []
        assert set(policy["permissions"]["deny"]) == {
            "read_file(*)", "write_file(*)", "command(*)", "unsandboxed(*)",
            "read_url(*)", "execute_url(*)", "mcp(*)",
        }
        return SubprocResult(command, 0, output({"status": "SUCCESS", "structured_output": verdict()},
                            cwd=root, tools=["write_to_file", "run_command"]), "", 0)

    monkeypatch.setattr(module, "run_async_command", cli)
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "requirement": requirement, "expected_commit_sha": "a" * 40},
    )
    assert result.success
    assert verification.await_count == 2


@pytest.mark.asyncio
async def test_review_requires_immutable_commit_before_model_call(monkeypatch, tmp_path):
    module = adapter()
    command = AsyncMock()
    monkeypatch.setattr(module, "run_async_command", command)
    result = await module.AntigravityReviewer().execute("Review", {"worktree_path": tmp_path})
    assert not result.success and "commit" in result.error
    command.assert_not_called()


@pytest.mark.asyncio
async def test_changed_checkout_rejects_even_valid_verdict(monkeypatch, tmp_path):
    module = adapter()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", AsyncMock(side_effect=[None, ValueError("checkout changed")]))
    monkeypatch.setattr(module, "run_async_command", AsyncMock(return_value=SubprocResult(
        [], 0, output({"status": "SUCCESS", "structured_output": verdict()}), "", 0,
    )))
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40},
    )
    assert not result.success and "checkout changed" in result.error


@pytest.mark.asyncio
async def test_model_mismatch_cannot_approve(monkeypatch, tmp_path):
    module = adapter()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", AsyncMock())
    async def cli(command, **kwargs):
        return SubprocResult(command, 0, output({"status": "SUCCESS", "structured_output": verdict()},
                            model="unexpected-model", cwd=kwargs["cwd"]), "", 0)
    monkeypatch.setattr(module, "run_async_command", cli)
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40},
    )
    assert not result.success and "model" in result.error


@pytest.mark.asyncio
async def test_changed_permission_profile_cannot_approve(monkeypatch, tmp_path):
    module = adapter()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", AsyncMock())

    async def cli(command, **kwargs):
        policy = Path(kwargs["env"]["HOME"]) / ".gemini/antigravity-cli/settings.json"
        policy.write_text('{}', encoding="utf-8")
        return SubprocResult(command, 0, output({"status": "SUCCESS", "structured_output": verdict()},
                            cwd=kwargs["cwd"], tools=["write_to_file"]), "", 0)

    monkeypatch.setattr(module, "run_async_command", cli)
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40,
                   "deny_policy_verified": True},
    )
    assert not result.success and "policy" in result.error


def test_standalone_parser_rejects_full_tool_registry_without_policy_proof():
    raw = output({"status": "SUCCESS", "structured_output": verdict()}, tools=["write_to_file"])
    with pytest.raises(ValueError):
        adapter().parse_antigravity_review(raw)


@pytest.mark.parametrize("sparse", [False, True])
def test_deny_policy_verification_accepts_equivalent_cli_serialization(tmp_path, sparse):
    module = adapter()
    expected = json.dumps(module.REVIEW_DENY_SETTINGS, sort_keys=True).encode("utf-8")
    rewritten = json.loads(expected)
    rewritten["permissions"]["deny"].reverse()
    rewritten["selectedModel"] = "gemini-3.8-flash-high"
    if sparse:
        rewritten.pop("toolPermission", None)
        rewritten.pop("allowNonWorkspaceAccess", None)
        rewritten["permissions"].pop("allow")
        rewritten["permissions"].pop("ask")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(rewritten, indent=2), encoding="utf-8")
    module._verify_deny_policy(path, expected)


@pytest.mark.parametrize("mutation", ["missing_deny", "weaker_deny", "allow", "ask", "auto_approve", "external_access"])
def test_deny_policy_verification_rejects_permission_weakening(tmp_path, mutation):
    module = adapter()
    expected = json.dumps(module.REVIEW_DENY_SETTINGS, sort_keys=True).encode("utf-8")
    changed = json.loads(expected)
    if mutation == "missing_deny":
        changed.pop("permissions")
    elif mutation == "weaker_deny":
        changed["permissions"]["deny"].pop()
    elif mutation in {"allow", "ask"}:
        changed["permissions"][mutation] = ["write_file(*)"]
    elif mutation == "auto_approve":
        changed["toolPermission"] = "always-proceed"
    else:
        changed["allowNonWorkspaceAccess"] = True
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError):
        module._verify_deny_policy(path, expected)


@pytest.mark.asyncio
async def test_native_deny_only_policy_persistence_can_approve(monkeypatch, tmp_path):
    module = adapter()
    monkeypatch.setattr(module.CodexExpert, "_verify_expected_head", AsyncMock())

    async def cli(command, **kwargs):
        path = Path(kwargs["env"]["HOME"]) / ".gemini/antigravity-cli/settings.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({"permissions": {"deny": current["permissions"]["deny"]}}, indent=2), encoding="utf-8")
        return SubprocResult(command, 0, output({"status": "SUCCESS", "structured_output": verdict()},
                            cwd=kwargs["cwd"], tools=["write_to_file", "run_command"]), "", 0)

    monkeypatch.setattr(module, "run_async_command", cli)
    result = await module.AntigravityReviewer(cli_cmd="fixture").execute(
        "Review", {"worktree_path": tmp_path, "expected_commit_sha": "a" * 40},
    )
    assert result.success, result.error
