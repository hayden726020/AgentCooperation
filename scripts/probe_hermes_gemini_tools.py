"""Run one real Hermes-to-Gemini read/edit/test task in a credential-free scratch workspace."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = PROJECT_ROOT / "hermes-agent"
for path in (PROJECT_ROOT, HERMES_SOURCE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

MODEL = "gemini-3.8-flash-high"
MARKER = "HERMES_GEMINI_PROJECT_OK"


@contextmanager
def temporary_environment():
    names = {
        "HERMES_HOME", "HERMES_RUNTIME_DIR", "USERPROFILE", "HOME",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    previous = {name: os.environ.get(name) for name in names}
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def probe_directory():
    root = PROJECT_ROOT / ".integration/hermes-gemini/tool-probes"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="probe-", dir=root, delete=False)


def close_scratch_log_handlers(root: Path) -> None:
    loggers = [logging.getLogger()] + [
        item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger)
    ]
    closed = set()
    for logger in loggers:
        for handler in list(logger.handlers):
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve().is_relative_to(root.resolve()):
                logger.removeHandler(handler)
                if handler not in closed:
                    handler.close()
                    closed.add(handler)


def error_category(error: object) -> str:
    detail = str(error).lower()
    for category, words in (
        ("location_unsupported", ("location", "region")),
        ("authentication", ("authentication", "unauthenticated", "login", "sign in")),
        ("quota", ("quota", "exhausted", "rate limit")),
        ("timeout", ("timeout", "timed out")),
        ("agent_configuration", ("unknown agent", "agent not found", "tool set")),
        ("project_policy", ("project policy", "permission policy", "protected path", "allowlist")),
        ("protocol", ("result", "response", "protocol", "model mismatch", "initialization")),
    ):
        if any(word in detail for word in words):
            return category
    return "other"


def probe(timeout: float) -> dict:
    from execution.proxy import get_cli_proxy_env

    with temporary_environment():
        os.environ.update(get_cli_proxy_env())
        return _probe_in_scratch(timeout)


def _probe_in_scratch(timeout: float) -> dict:
    with probe_directory() as directory:
        root = Path(directory)
        if os.name == "nt":
            import subprocess
            subprocess.run(["icacls", str(root), "/grant", "*S-1-15-2-1:(OI)(CI)(RX,W)"], capture_output=True, check=False)
        hermes_home = root / "hermes"
        cli_home = root / "native"
        workspace = root / "workspace"
        for path in (hermes_home, cli_home, workspace):
            path.mkdir()
        target = workspace / "stage4_probe.txt"
        target.write_text("before\n", encoding="utf-8")
        test_file = workspace / "test_stage4_probe.py"
        test_file.write_text(
            "from pathlib import Path\n\n"
            "def test_stage4_edit():\n"
            "    assert Path(__file__).with_name('stage4_probe.txt').read_text(encoding='utf-8') == 'after\\n'\n",
            encoding="utf-8",
        )

        import yaml

        (hermes_home / "config.yaml").write_text(yaml.safe_dump({
            "model": {"provider": "antigravity-cli", "default": MODEL},
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
        os.environ.update({
            "HERMES_HOME": str(hermes_home),
            "HERMES_RUNTIME_DIR": str(root / "runtime"),
            "USERPROFILE": str(cli_home),
            "HOME": str(cli_home),
        })
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
            os.environ.pop(name, None)

        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from agent.runtime_cwd import reset_session_cwd, set_session_cwd
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        home_token = set_hermes_home_override(hermes_home)
        cwd_token = set_session_cwd(str(workspace))
        agent = None
        watchdog = None
        try:
            runtime = resolve_runtime_provider(requested="antigravity-cli", target_model=MODEL)
            agent = AIAgent(
                provider=runtime["provider"], api_mode=runtime["api_mode"],
                base_url=runtime["base_url"], api_key=runtime["api_key"],
                command=runtime["command"], args=runtime["args"], model=MODEL,
                cwd=str(workspace), enabled_toolsets=[], max_iterations=1,
                skip_context_files=True, skip_memory=True, skip_background_review=True,
                load_soul_identity=False, run_budget_seconds=timeout, quiet_mode=True,
                save_trajectories=False,
            )
            client = agent.client
            if type(client).__name__ != "AntigravityCLIClient" or client.project_execution is None:
                raise RuntimeError("Hermes did not construct the project-enabled CLI provider")
            watchdog = threading.Timer(timeout, client.close)
            watchdog.daemon = True
            watchdog.start()
            result = agent.run_conversation(
                "Use view_file to read stage4_probe.txt. Replace its complete content with the exact line "
                "after using replace_file_content. Then run exactly "
                "python -m pytest test_stage4_probe.py -q from the active workspace. "
                f"Only after the test passes, reply exactly {MARKER}."
            )
            response = str(result.get("final_response", "")).strip()
            tool_results = list(client.last_native_tool_results or getattr(type(client), "_global_native_tool_results", []))
            succeeded_tools = {item["name"] for item in tool_results if item.get("succeeded")}
            edited = target.read_text(encoding="utf-8") == "after\n"
            required_tools = {"view_file", "replace_file_content", "run_command"}
            summary = {
                "provider": runtime["provider"],
                "model_requested": MODEL,
                "client": type(client).__name__,
                "completed": bool(result.get("completed")),
                "marker_received": response == MARKER,
                "file_edit_verified": edited,
                "required_tool_steps_verified": required_tools.issubset(succeeded_tools),
                "native_tool_steps": tool_results,
                "error_present": bool(result.get("error")),
                "error_category": error_category(result.get("error")) if result.get("error") else None,
                "response": response if response == MARKER else "[unexpected response withheld]",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            (root / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return summary
        finally:
            if watchdog is not None:
                watchdog.cancel()
            try:
                if agent is not None:
                    agent.close()
            finally:
                reset_session_cwd(cwd_token)
                reset_hermes_home_override(home_token)
                close_scratch_log_handlers(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 300:
        parser.error("timeout must be between 10 and 300 seconds")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = probe(args.timeout)
    except Exception as exc:
        print(json.dumps({
            "passed": False,
            "error_type": type(exc).__name__,
            "error_category": error_category(exc),
        }))
        return 1
    passed = all((
        result["completed"], result["marker_received"], result["file_edit_verified"],
        result["required_tool_steps_verified"], not result["error_present"],
    ))
    print(json.dumps({"passed": passed, **result}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
