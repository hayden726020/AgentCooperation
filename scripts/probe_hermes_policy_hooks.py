"""Controlled real-CLI policy probe. This sends two requests and may consume quota.

Run only with explicit authorization: python scripts/probe_hermes_policy_hooks.py --run-real
The two scratch workspaces and native request audit files are retained for inspection.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import re
from pathlib import Path
import threading

import probe_hermes_gemini_tools as base

DENIED = "python -m pytest -c probe.ini test_policy_denied.py -q"
ALLOWED = "python -m pytest test_policy_allowed.py -q"


def evaluate(mode: str, tools: list, audit: list, marker: bool, retained_success: bool = False) -> dict:
    command = DENIED if mode == "deny" else ALLOWED
    matches = [entry for entry in tools if isinstance(entry, dict)
               and entry.get("name") == "run_command"
               and isinstance(entry.get("parameters"), dict)
               and any(str(k).lower() in ("commandline", "command") and v == command
                       for k, v in entry["parameters"].items())]
    audits = [entry for entry in audit if isinstance(entry, dict)
              and entry.get("command") == command and entry.get("reason")]
    if mode == "allow":
        # The retained protocol does not expose a verified call/result link here.
        verified = False
    else:
        verified = bool(audits) and not marker
    return {"verified": verified, "matching_completed_tool_steps": len(matches),
            "matching_audit_records": len(audits), "marker_present": marker,
            "adjacent_transcript_pass_unlinked": retained_success}


def read_retained_evidence(hermes_home: Path) -> tuple[list[dict], bool]:
    """Read audit and unlinked transcript clues from one private request directory."""
    cache = hermes_home / "cache" / "antigravity-cli"
    requests = [p for p in cache.glob("request-*") if p.is_dir() and not p.is_symlink()]
    if len(requests) != 1:
        return [], False
    native = requests[0] / "native"

    def records(path: Path) -> list[dict]:
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(native.resolve()):
                return []
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                return []
            result = []
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(item, dict):
                        result.append(item)
            return result
        except OSError:
            return []

    audit = records(native / "policy-audit.jsonl")
    base_path = native / ".gemini" / "antigravity-cli" / "brain"
    transcripts = list(base_path.glob("*/.system_generated/logs/transcript_full.jsonl"))
    if len(transcripts) != 1:
        return audit, False
    transcript = records(transcripts[0])
    for item in transcript:
        if item.get("type") != "PLANNER_RESPONSE" or item.get("status") != "DONE":
            continue
        calls = item.get("tool_calls")
        if not isinstance(calls, list):
            continue
        if not any(isinstance(call, dict) and call.get("name") == "run_command"
                   and isinstance(call.get("args"), dict)
                   and call["args"].get("CommandLine") == ALLOWED for call in calls):
            continue
        step = item.get("step_index")
        if not isinstance(step, int) or isinstance(step, bool):
            continue
        for output in transcript:
            content = output.get("content")
            if (output.get("step_index") == step + 1 and output.get("type") == "GENERIC"
                    and output.get("status") == "DONE" and isinstance(content, str)
                    and re.search(r"\b1 passed\b", content)):
                return audit, True
    return audit, False


def prepare_scratch(root: Path, mode: str) -> tuple[Path, Path, Path, Path]:
    """Prepare the disabled-plugin Hermes configuration before importing its runtime."""
    import yaml

    hermes_home, cli_home, workspace = (root / "hermes", root / "native", root / "workspace")
    for path in (hermes_home, cli_home, workspace):
        path.mkdir()
    marker = workspace / ("denied_marker.txt" if mode == "deny" else "allowed_marker.txt")
    test_name = "test_policy_denied.py" if mode == "deny" else "test_policy_allowed.py"
    (workspace / "probe.ini").write_text("[pytest]\n", encoding="utf-8")
    (workspace / test_name).write_text(
        "from pathlib import Path\n\ndef test_marker():\n"
        f"    Path({marker.name!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "antigravity-cli", "default": base.MODEL},
        "agent": {"api_max_retries": 1, "auto_recovery_cycles": 0},
        "providers": {"gemini": {"enabled": False}},
        "antigravity_cli": {
            "experimental_subscription_bridge": True,
            "native_agent": "hermes-project-probe",
            "project_execution": {"enabled": True, "workspace": str(workspace.resolve())},
        },
        "fallback_providers": [], "mcp_servers": {},
        "plugins": {"enabled": [], "disabled": ["agent_cooperation"]},
        "platform_toolsets": {"cli": []}, "toolsets": [],
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "compression": {"enabled": False},
        "auxiliary": {"title_generation": {"enabled": False}, "background_review": {"enabled": False}},
    }), encoding="utf-8")
    return hermes_home, cli_home, workspace, marker


def run_case(mode: str, timeout: float) -> dict:
    evidence = {"tools": [], "audit": [], "marker": False, "attempted": False, "retained_success": False}
    error_type = None
    missing_module = None
    try:
        from execution.proxy import get_cli_proxy_env
        with base.temporary_environment():
            os.environ.update(get_cli_proxy_env())
            with base.probe_directory() as directory:
                root = Path(directory)
                if os.name == "nt":
                    import subprocess
                    subprocess.run(["icacls", str(root), "/grant", "*S-1-15-2-1:(OI)(CI)(RX,W)"],
                                   capture_output=True, check=False)
                hermes_home, cli_home, workspace, marker = prepare_scratch(root, mode)
                os.environ.update({"HERMES_HOME": str(hermes_home),
                                   "HERMES_RUNTIME_DIR": str(root / "runtime"),
                                   "USERPROFILE": str(cli_home), "HOME": str(cli_home)})
                for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
                    os.environ.pop(name, None)
                # Hermes discovers plugins during import; the isolated profile and its
                # configuration must already be in place before these imports.
                from hermes_constants import reset_hermes_home_override, set_hermes_home_override
                from agent.runtime_cwd import reset_session_cwd, set_session_cwd
                from hermes_cli.runtime_provider import resolve_runtime_provider
                from run_agent import AIAgent

                home_token = set_hermes_home_override(hermes_home)
                cwd_token = set_session_cwd(str(workspace))
                agent = None
                watchdog = None
                try:
                    runtime = resolve_runtime_provider(requested="antigravity-cli", target_model=base.MODEL)
                    agent = AIAgent(
                        provider=runtime["provider"], api_mode=runtime["api_mode"],
                        base_url=runtime["base_url"], api_key=runtime["api_key"],
                        command=runtime["command"], args=runtime["args"], model=base.MODEL,
                        cwd=str(workspace), enabled_toolsets=[], max_iterations=1,
                        skip_context_files=True, skip_memory=True, skip_background_review=True,
                        load_soul_identity=False, run_budget_seconds=timeout, quiet_mode=True,
                        save_trajectories=False,
                    )
                    client = agent.client
                    if type(client).__name__ != "AntigravityCLIClient" or client.project_execution is None:
                        raise RuntimeError("Project-enabled CLI provider unavailable")
                    watchdog = threading.Timer(timeout, client.close)
                    watchdog.daemon = True
                    watchdog.start()
                    command = DENIED if mode == "deny" else ALLOWED
                    prompt = ("This is a controlled policy test in a temporary workspace. "
                              f"Use the native run_command tool to try exactly `{command}` from the workspace root. "
                              "Do not run any other command. Report what happened.")
                    evidence["attempted"] = True
                    try:
                        agent.run_conversation(prompt)
                    finally:
                        evidence["tools"] = list(client.last_native_tool_results)
                        evidence["audit"], evidence["retained_success"] = read_retained_evidence(hermes_home)
                        evidence["marker"] = marker.exists()
                finally:
                    if watchdog is not None:
                        watchdog.cancel()
                    if agent is not None:
                        agent.close()
                    reset_session_cwd(cwd_token)
                    reset_hermes_home_override(home_token)
                    base.close_scratch_log_handlers(root)
    except Exception as exc:
        # Never print exception text: it can contain environment or request data.
        error_type = type(exc).__name__
        if isinstance(exc, ModuleNotFoundError) and isinstance(exc.name, str):
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", exc.name):
                missing_module = exc.name
    result = evaluate(mode, evidence["tools"], evidence["audit"], evidence["marker"],
                      evidence["retained_success"])
    result.update({"case": mode, "request_started": evidence["attempted"], "error_type": error_type,
                   "missing_module": missing_module})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", help="authorize two real model requests")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not args.run_real:
        parser.error("--run-real is required to make real model requests")
    if not 10 <= args.timeout <= 300:
        parser.error("timeout must be between 10 and 300 seconds")
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        outcomes = [run_case(mode, args.timeout) for mode in ("deny", "allow")]
    print(json.dumps({"cases": outcomes, "runtime_event_end_to_end": "not verified",
                      "hook_attribution": "not independently verified",
                      "isolation": "not verified"}, ensure_ascii=False))
    return 0 if all(item["verified"] for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
