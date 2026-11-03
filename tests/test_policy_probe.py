from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import probe_hermes_policy_hooks as probe


def test_deny_requires_matching_audit_and_no_execution_marker():
    audit = [{"command": probe.DENIED, "reason": "policy"}]
    assert probe.evaluate("deny", [], audit, False)["verified"]
    assert not probe.evaluate("deny", [], [], False)["verified"]
    assert not probe.evaluate("deny", [], audit, True)["verified"]
    assert not probe.evaluate("deny", [], [{"command": "different", "reason": "policy"}], False)["verified"]


def test_allow_requires_actual_matching_success_and_marker():
    steps = [{"name": "run_command", "succeeded": True,
              "parameters": {"CommandLine": probe.ALLOWED}}]
    assert not probe.evaluate("allow", steps, [], True)["verified"]
    assert not probe.evaluate("allow", steps, [], False)["verified"]
    assert not probe.evaluate("allow", [], [], True)["verified"]
    steps[0]["succeeded"] = False
    assert not probe.evaluate("allow", steps, [], True)["verified"]


def test_scratch_prepares_isolated_profile_and_inert_fixture_before_runtime(tmp_path):
    import yaml
    hermes_home, cli_home, workspace, marker = probe.prepare_scratch(tmp_path, "deny")
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["disabled"] == ["agent_cooperation"]
    assert config["antigravity_cli"]["project_execution"]["workspace"] == str(workspace.resolve())
    assert cli_home.is_dir()
    assert (workspace / "test_policy_denied.py").is_file()
    assert not marker.exists()


def test_retained_evidence_matches_command_and_real_tool_output(tmp_path):
    import json
    native = tmp_path / "cache" / "antigravity-cli" / "request-one" / "native"
    log = native / ".gemini" / "antigravity-cli" / "brain" / "session" / ".system_generated" / "logs"
    log.mkdir(parents=True)
    (native / "policy-audit.jsonl").write_text(json.dumps({"command": probe.DENIED, "reason": "denied"}) + "\n")
    rows = [
        {"type": "PLANNER_RESPONSE", "status": "DONE", "step_index": 1,
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": probe.ALLOWED}}]},
        {"type": "GENERIC", "status": "DONE", "step_index": 2, "content": "1 passed in 0.1s"},
    ]
    transcript = log / "transcript_full.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in rows))
    audit, passed = probe.read_retained_evidence(tmp_path)
    assert probe.evaluate("deny", [], audit, False)["verified"]
    # Adjacent transcript records are clues, not proof of a linked tool result.
    assert not probe.evaluate("allow", [], audit, True, passed)["verified"]
    rows[1]["step_index"] = 3
    transcript.write_text("\n".join(json.dumps(row) for row in rows))
    assert not probe.read_retained_evidence(tmp_path)[1]
