"""Single real Hermes conversation probe via the subscription CLI, never an API key."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from contextlib import contextmanager
from contextlib import redirect_stdout, redirect_stderr
import io
import logging
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "hermes-agent"))

MODEL = "gemini-3.8-flash-high"
MARKER = "HERMES_GEMINI_OK"


@contextmanager
def temporary_environment(updates: dict[str, str | None]):
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def probe_directory():
    # Retain local run evidence: on Windows global DB/log handles may outlive
    # an Agent, and cleanup failures must never replace the model's result.
    base = PROJECT_ROOT / ".integration/hermes-gemini/probes"
    base.mkdir(parents=True, exist_ok=True)
    yield tempfile.mkdtemp(prefix="probe-", dir=base)


def probe(timeout: float) -> dict:
    environment_names = ("HERMES_HOME", "HERMES_RUNTIME_DIR", "USERPROFILE", "HOME",
                         "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL",
                         "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    with temporary_environment({name: os.environ.get(name) for name in environment_names}):
        from execution.proxy import get_cli_proxy_env
        # Reuse the user's existing network configuration; never select a new proxy.
        proxy_env = get_cli_proxy_env()
        os.environ.update(proxy_env)
        return _probe_in_scratch(timeout)


def error_category(error: object) -> str:
    detail = str(error).lower()
    for category, words in (
        ("location_unsupported", ("location", "region")),
        ("authentication", ("authentication", "unauthenticated", "login", "sign in", "sign-in")),
        ("quota", ("quota", "exhausted", "rate limit")),
        ("timeout", ("timeout", "timed out")),
        ("agent_configuration", ("unknown agent", "agent not found", "agent profile")),
        ("protocol", ("result", "response", "protocol", "model mismatch")),
    ):
        if any(word in detail for word in words):
            return category
    return "other"


def _probe_in_scratch(timeout: float) -> dict:
    # Only a scratch conversation is created. Personal authentication stays in
    # the OS keyring; no auth files or API credentials are copied into this home.
    with probe_directory() as directory:
        root = Path(directory)
        if not root.resolve().is_relative_to(PROJECT_ROOT.resolve()):
            raise RuntimeError("Probe scratch directory is outside the workspace")
        hermes_home = root / "hermes"
        cli_home = root / "native"
        workspace = root / "workspace"
        for path in (hermes_home, cli_home, workspace):
            path.mkdir()
        import yaml
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({
            "model": {"provider": "antigravity-cli", "default": MODEL},
            "agent": {"api_max_retries": 1, "auto_recovery_cycles": 0},
            "providers": {"gemini": {"enabled": False}},
            "antigravity_cli": {"experimental_subscription_bridge": True, "native_agent": "hermes-gemini-probe"},
            "fallback_providers": [], "mcp_servers": {},
            "plugins": {"enabled": [], "disabled": ["agent_cooperation"]},
            "platform_toolsets": {"cli": []}, "toolsets": [],
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "compression": {"enabled": False},
            "auxiliary": {"title_generation": {"enabled": False}, "background_review": {"enabled": False}},
        }), encoding="utf-8")
        settings = cli_home / ".gemini/antigravity-cli/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": [], "ask": [], "deny": [
                "read_file(*)", "write_file(*)", "command(*)", "unsandboxed(*)",
                "read_url(*)", "execute_url(*)", "mcp(*)",
            ]}, "toolPermission": "strict", "allowNonWorkspaceAccess": False,
            "useG1Credits": False,
        }), encoding="utf-8")
        native_agent = workspace / ".agents/agents/hermes-gemini-probe.md"
        native_agent.parent.mkdir(parents=True)
        native_agent.write_text(
            "---\nname: hermes-gemini-probe\ndescription: No-tool connection check\n"
            "tools: []\nmainAgent: true\nsubagent: false\ncommandExecutionPolicy: 'off'\n"
            "mcpServers: []\nskills: []\nplugins: []\n---\n"
            "Reply to the supplied message without using any tool or subagent.\n",
            encoding="utf-8",
        )
        os.environ.update({"HERMES_HOME": str(hermes_home), "HERMES_RUNTIME_DIR": str(root / "runtime"),
                           "USERPROFILE": str(cli_home), "HOME": str(cli_home)})
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
            os.environ.pop(name, None)
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(hermes_home)
        agent = None
        cwd_token = None
        watchdog = None
        try:
            from agent.runtime_cwd import set_session_cwd, reset_session_cwd
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent
            runtime = resolve_runtime_provider(requested="antigravity-cli", target_model=MODEL)
            cwd_token = set_session_cwd(str(workspace))
            agent = AIAgent(
                provider=runtime["provider"], api_mode=runtime["api_mode"],
                base_url=runtime["base_url"], api_key=runtime["api_key"],
                command=runtime["command"], args=runtime["args"], model=MODEL,
                cwd=str(workspace), enabled_toolsets=[], max_iterations=1,
                skip_context_files=True, skip_memory=True, skip_background_review=True,
                load_soul_identity=False, run_budget_seconds=timeout, quiet_mode=True,
                save_trajectories=False,
            )
            if type(agent.client).__name__ != "AntigravityCLIClient":
                raise RuntimeError("Hermes did not construct the intended CLI provider")
            if not getattr(agent.client, "subscription_bridge_enabled", False):
                raise RuntimeError("Hermes did not apply the experimental profile configuration")
            agent.quiet_mode = True
            watchdog = threading.Timer(timeout, lambda: agent.client.close())
            watchdog.daemon = True
            watchdog.start()
            result = agent.run_conversation(f"Reply exactly {MARKER}. Do not use tools or subagents.")
            response = str(result.get("final_response", "")).strip()
            summary = {"provider": runtime["provider"], "model_requested": MODEL,
                    "client": type(agent.client).__name__, "completed": bool(result.get("completed")),
                    "marker_received": response == MARKER, "error_present": bool(result.get("error")),
                    "error_category": error_category(result.get("error")) if result.get("error") else None,
                    "response": response if response == MARKER else "[unexpected response withheld]",
                    "checked_at": datetime.now(timezone.utc).isoformat()}
            (root / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return summary
        finally:
            if watchdog is not None:
                watchdog.cancel()
            try:
                if agent is not None:
                    agent.close()
            finally:
                if cwd_token is not None:
                    reset_session_cwd(cwd_token)
                reset_hermes_home_override(token)
                close_scratch_log_handlers(root)


def close_scratch_log_handlers(root: Path) -> None:
    """Release only this probe's log files before Windows removes its scratch home."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be between 1 and 120 seconds")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = probe(args.timeout)
    except Exception as exc:
        # Never print raw client errors or logs containing account identifiers.
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "error_category": error_category(exc),
                          "resource": Path(exc.filename).name if isinstance(exc, OSError) and exc.filename else None}))
        return 1
    passed = result["completed"] and result["marker_received"] and not result["error_present"]
    print(json.dumps({"passed": passed, **result}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
