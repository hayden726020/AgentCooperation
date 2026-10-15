"""Offline contracts for the explicitly enabled Hermes Gemini project-tool mode.

These tests use the real provider/profile boundary and a fake native process.  They
must never contact Antigravity or execute a reported model tool.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = PROJECT_ROOT / "hermes-agent"
SETUP_SCRIPT = PROJECT_ROOT / "scripts/configure_hermes_bots.py"
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from providers import get_provider_profile


PROJECT_TOOLS = {
    "view_file",
    "grep_search",
    "write_to_file",
    "replace_file_content",
    "run_command",
}


class InputPipe(io.StringIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, events, *, exit_code=0):
        self.pid = 940001
        self.stdin = InputPipe()
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO("")
        self.returncode = None
        self.exit_code = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert self.stdin.closed, "stream-json input must be closed before waiting"
        self.returncode = self.exit_code
        return self.returncode


def init_event(workspace: Path, agent: str, *, tools=PROJECT_TOOLS):
    return {
        "event": "init",
        "init": {
            "model": "gemini-3.8-flash-high",
            "cwd": str(workspace.resolve()),
            "agent": agent,
            "tools": sorted(tools),
        },
    }


def tool_event(name: str, arguments: dict, *, info_name: str | None = None,
               argument_field: str = "parameters"):
    return {
        "event": "step_update",
        "step_update": {
            "step_type": "tool",
            "state": "DONE",
            "tool_name": name,
            "tool_info": {
                "name": info_name or name,
                argument_field: arguments,
                "status": "completed",
            },
        },
    }


def result_event(response="done"):
    return {"event": "result", "result": {"status": "SUCCESS", "response": response}}


def valid_tool_arguments(name: str, workspace: Path) -> dict:
    if name == "view_file":
        return {"AbsolutePath": str(workspace / "README.md")}
    if name == "grep_search":
        return {"Query": "project_execution", "SearchPath": str(workspace / "tests")}
    if name == "write_to_file":
        return {"TargetFile": str(workspace / "src/new_module.py"), "CodeContent": "VALUE = 1\n"}
    if name == "replace_file_content":
        return {"TargetFile": str(workspace / "src/module.py"), "TargetContent": "old", "ReplacementContent": "new"}
    if name == "run_command":
        return {"CommandLine": "python -m pytest -q", "Cwd": str(workspace)}
    raise AssertionError(f"unsupported test tool: {name}")


@pytest.fixture
def setup_module():
    spec = importlib.util.spec_from_file_location("hermes_bot_setup_project_tools", SETUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def provider_context(tmp_path, monkeypatch):
    profile_home = tmp_path / "hermes"
    native_home = tmp_path / "native"
    workspace = tmp_path / "project"
    profile_home.mkdir()
    workspace.mkdir()
    native_settings = native_home / ".gemini/antigravity-cli/settings.json"
    native_settings.parent.mkdir(parents=True)
    native_settings.write_text('{"modelProvider":"antigravity"}', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(native_home))
    monkeypatch.setenv("HOME", str(native_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    token = set_hermes_home_override(profile_home)
    try:
        profile = get_provider_profile("antigravity-cli")
        probe = profile.create_client()
        module = sys.modules[probe.__class__.__module__]
        probe.close()
        yield profile, module, profile_home, workspace
    finally:
        reset_hermes_home_override(token)


def project_client(profile, workspace: Path):
    return profile.create_client(
        subscription_bridge_enabled=True,
        native_agent="hermes-project-dev",
        project_execution={"enabled": True, "workspace": str(workspace.resolve())},
    )


def invoke(client, module, process):
    with patch.object(module.subprocess, "Popen", return_value=process) as spawn:
        with patch.object(module, "kill_process_tree", side_effect=lambda _: setattr(process, "returncode", -1)):
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": "make the requested project change"}],
            )
    return response, spawn


def profile_files(home: Path, *names: str):
    return {
        path: path.read_bytes()
        for name in names
        for path in (home / "profiles" / name).iterdir()
        if path.is_file()
    }


def test_project_execution_is_disabled_by_default_and_retains_no_tool_isolation(provider_context):
    profile, module, profile_home, workspace = provider_context
    client = profile.create_client(
        subscription_bridge_enabled=True,
        cwd=str(workspace),
        native_agent="hermes-project-dev",
    )
    process = FakeProcess([
        {"event": "init", "init": {"model": "gemini-3.8-flash-high"}},
        result_event(),
    ])
    try:
        _, spawn = invoke(client, module, process)
    finally:
        client.close()
    launched = spawn.call_args
    assert Path(launched.kwargs["cwd"]) != workspace.resolve()
    assert Path(launched.kwargs["cwd"]).is_relative_to(profile_home)
    assert "--sandbox" not in launched.args[0]
    agent_name = launched.args[0][launched.args[0].index("--agent") + 1]
    agent = Path(launched.kwargs["cwd"]) / ".agents/agents" / f"{agent_name}.md"
    assert yaml.safe_load(agent.read_text(encoding="utf-8").split("---")[1])["tools"] == []


@pytest.mark.parametrize("configured", [
    {"enabled": True},
    {"enabled": True, "workspace": "relative/project"},
    {"enabled": True, "workspace": ""},
])
def test_project_execution_requires_strict_opt_in_and_an_absolute_workspace(provider_context, configured):
    profile, _, _, _ = provider_context
    with pytest.raises(ValueError, match="project|workspace|absolute"):
        profile.create_client(
            subscription_bridge_enabled=True,
            native_agent="hermes-project-dev",
            project_execution=configured,
        )


def test_non_boolean_project_opt_in_remains_disabled(provider_context):
    profile, module, profile_home, workspace = provider_context
    client = profile.create_client(
        subscription_bridge_enabled=True,
        native_agent="hermes-project-dev",
        project_execution={"enabled": "true", "workspace": str(workspace.resolve())},
    )
    process = FakeProcess([
        {"event": "init", "init": {"model": "gemini-3.8-flash-high"}},
        result_event(),
    ])
    try:
        _, spawn = invoke(client, module, process)
    finally:
        client.close()
    assert "--sandbox" not in spawn.call_args.args[0]
    assert Path(spawn.call_args.kwargs["cwd"]).is_relative_to(profile_home)


def test_project_execution_rejects_custom_command_policy(provider_context):
    profile, _, _, workspace = provider_context
    with pytest.raises(ValueError, match="(?i)command|fixed|project"):
        profile.create_client(
            subscription_bridge_enabled=True,
            native_agent="hermes-project-dev",
            project_execution={
                "enabled": True,
                "workspace": str(workspace.resolve()),
                "allowed_commands": ["pwsh"],
            },
        )


def test_effective_profile_configuration_enables_fixed_project_workspace(provider_context):
    profile, _, profile_home, workspace = provider_context
    (profile_home / "config.yaml").write_text(yaml.safe_dump({
        "antigravity_cli": {
            "experimental_subscription_bridge": True,
            "native_agent": "hermes-project-dev",
            "project_execution": {"enabled": True, "workspace": str(workspace.resolve())},
        },
    }), encoding="utf-8")
    client = profile.create_client()
    process = FakeProcess([init_event(workspace, client.native_agent), result_event()])
    try:
        _, spawn = invoke(client, sys.modules[client.__class__.__module__], process)
    finally:
        client.close()
    assert Path(spawn.call_args.kwargs["cwd"]) == workspace.resolve()
    if os.name != "nt":
        assert "--sandbox" in spawn.call_args.args[0]
    else:
        assert "--sandbox" not in spawn.call_args.args[0]


def test_project_mode_uses_fixed_workspace_sandbox_and_narrow_native_policy(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    agent_name = client.native_agent
    process = FakeProcess([init_event(workspace, agent_name), result_event()])
    try:
        _, spawn = invoke(client, module, process)
    finally:
        client.close()

    launched = spawn.call_args
    command = launched.args[0]
    assert Path(launched.kwargs["cwd"]) == workspace.resolve()
    if os.name != "nt":
        assert "--sandbox" in command
    else:
        assert "--sandbox" not in command
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--agent") + 1] == agent_name

    native_home = Path(launched.kwargs["env"]["HOME"])
    settings = json.loads((native_home / ".gemini/antigravity-cli/settings.json").read_text(encoding="utf-8"))
    assert settings["allowNonWorkspaceAccess"] is False
    expected_permission = "always-proceed" if os.name == "nt" else "proceed-in-sandbox"
    assert settings["toolPermission"] == expected_permission
    allow = set(settings["permissions"]["allow"])
    deny = set(settings["permissions"]["deny"])
    assert any("read" in item.lower() and "file" in item.lower() for item in allow)
    assert any("write" in item.lower() and "file" in item.lower() for item in allow)
    assert any("python" in item.lower() and "pytest" in item.lower() for item in allow)
    assert "command(git status)" in allow
    assert "unsandboxed(git status)" in allow
    assert not any("git diff" in item.lower() or "git status " in item.lower() for item in allow)
    assert not any("git status" in item.lower() and "*" in item for item in allow)
    assert any("mcp" in item.lower() for item in deny)
    assert any("url" in item.lower() or "web" in item.lower() for item in deny)
    assert any("unsandboxed" in item.lower() for item in deny)
    assert any(".git" in item.lower() and "write" in item.lower() for item in deny)
    for protected in (".git", ".env", ".aws", ".codex"):
        assert any(protected in item.lower() and "read" in item.lower() for item in deny)
        assert any(protected in item.lower() and "write" in item.lower() for item in deny)

    agent = native_home / ".gemini/config/agents" / agent_name / "agent.md"
    frontmatter = yaml.safe_load(agent.read_text(encoding="utf-8").split("---")[1])
    assert set(frontmatter["tools"]) == PROJECT_TOOLS
    assert frontmatter["mainAgent"] is True and frontmatter["subagent"] is False
    assert frontmatter["mcpServers"] == []
    assert frontmatter["skills"] == [] and frontmatter["plugins"] == []


def test_project_permission_policy_is_reverified_after_native_process(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([init_event(workspace, client.native_agent), result_event()])

    def tampering_spawn(*args, **kwargs):
        settings_path = Path(kwargs["env"]["HOME"]) / ".gemini/antigravity-cli/settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["permissions"]["allow"].append("command(pwsh *)")
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        return process

    try:
        with patch.object(module.subprocess, "Popen", side_effect=tampering_spawn):
            with patch.object(module, "kill_process_tree", side_effect=lambda _: setattr(process, "returncode", -1)):
                with pytest.raises(RuntimeError, match="(?i)permission|policy|allow"):
                    client.chat.completions.create(messages=[{"role": "user", "content": "work"}])
    finally:
        client.close()


def test_pretool_hook_enforces_project_boundary_before_native_tool_execution(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([init_event(workspace, client.native_agent), result_event()])
    try:
        _, spawn = invoke(client, module, process)
    finally:
        client.close()

    native_home = Path(spawn.call_args.kwargs["env"]["HOME"])
    hooks_path = native_home / ".gemini/config/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    pretool = hooks["hermes-project-policy"]["PreToolUse"]
    assert len(pretool) == 1 and pretool[0]["matcher"] == "*"
    validator = native_home / "hermes-project-policy.py"
    assert validator.is_file()

    cases = [
        ("allow", {"name": "view_file", "args": {"AbsolutePath": str(workspace / "README.md")}}),
        ("allow", {"name": "run_command", "args": {"CommandLine": "python -m pytest -q", "Cwd": str(workspace)}}),
        ("allow", {"name": "run_command", "args": {"CommandLine": "git status", "Cwd": str(workspace)}}),
        ("deny", {"name": "write_to_file", "args": {"TargetFile": str(workspace / ".git/config")}}),
        ("deny", {"name": "read_url", "args": {"url": "https://example.invalid"}}),
        ("deny", {"name": "mcp", "args": {"server": "fixture"}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git status --short", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git -c alias.status=!whoami status", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git --git-dir=../other/.git status", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git -C .. status", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git.exe status", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git  status", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "git\tstatus", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "python -m pytest ..", "Cwd": str(workspace)}}),
        ("deny", {"name": "run_command", "args": {"CommandLine": "python -m pytest", "Cwd": str(workspace.parent)}}),
        ("deny", {"name": "view_file", "args": {"AbsolutePath": str(workspace / "subdir/.env")}}),
        ("deny", {"name": "view_file", "args": {"AbsolutePath": str(workspace / ".env.production")}}),
        ("deny", {"name": "write_to_file", "args": {"TargetFile": str(workspace / "config/auth.json"), "CodeContent": "{}"}}),
        ("deny", {"name": "view_file", "args": {"AbsolutePath": str(workspace / "keys/id_rsa")}}),
        ("deny", {"name": "view_file", "args": {"AbsolutePath": str(workspace / "nested/.git/HEAD")}}),
    ]
    for expected, call in cases:
        completed = subprocess.run(
            [sys.executable, str(validator)],
            input=json.dumps({"toolCall": call}),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        decision = json.loads(completed.stdout)
        assert decision["decision"] == expected, call


def test_pretool_hook_configuration_is_reverified_after_native_process(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([init_event(workspace, client.native_agent), result_event()])

    def tampering_spawn(*args, **kwargs):
        hooks_path = Path(kwargs["env"]["HOME"]) / ".gemini/config/hooks.json"
        hooks_path.write_text("{}", encoding="utf-8")
        return process

    try:
        with patch.object(module.subprocess, "Popen", side_effect=tampering_spawn):
            with patch.object(module, "kill_process_tree", side_effect=lambda _: setattr(process, "returncode", -1)):
                with pytest.raises(RuntimeError, match="(?i)hook|policy|permission"):
                    client.chat.completions.create(messages=[{"role": "user", "content": "work"}])
    finally:
        client.close()


@pytest.mark.parametrize("missing", ["tools", "cwd", "agent"])
def test_project_init_must_attest_tools_workspace_and_agent_before_use(provider_context, missing):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    initialization = init_event(workspace, client.native_agent)
    del initialization["init"][missing]
    process = FakeProcess([
        initialization,
        tool_event("view_file", {"AbsolutePath": str(workspace / "README.md")}),
        result_event(),
    ])
    try:
        with pytest.raises((RuntimeError, ValueError), match="initial|tools|cwd|agent|mismatch"):
            invoke(client, module, process)
    finally:
        client.close()


def test_project_tool_event_before_validated_init_is_rejected(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([
        tool_event("view_file", {"path": "README.md"}),
        init_event(workspace, client.native_agent),
        result_event(),
    ])
    try:
        with pytest.raises(RuntimeError, match="initial|step"):
            invoke(client, module, process)
    finally:
        client.close()


@pytest.mark.parametrize("mismatch", ["missing_tool", "cwd", "agent"])
def test_project_init_rejects_inconsistent_toolset_workspace_or_agent(provider_context, mismatch):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    initialization = init_event(workspace, client.native_agent)
    if mismatch == "missing_tool":
        initialization["init"]["tools"].remove("run_command")
    elif mismatch == "cwd":
        initialization["init"]["cwd"] = str(workspace.parent)
    else:
        initialization["init"]["agent"] = "other-agent"
    process = FakeProcess([initialization, result_event()])
    try:
        with pytest.raises((RuntimeError, ValueError), match="(?i)tool|cwd|agent|mismatch"):
            invoke(client, module, process)
    finally:
        client.close()


def test_project_init_accepts_native_tools_superset(provider_context):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    initialization = init_event(workspace, client.native_agent, tools=PROJECT_TOOLS | {"search_web", "read_url_content"})
    process = FakeProcess([initialization, result_event()])
    try:
        response, _ = invoke(client, module, process)
    finally:
        client.close()
    assert response.choices[0].message.content == "done"


@pytest.mark.parametrize(("name", "command", "argument_field"), [
    ("view_file", None, "parameters"),
    ("view_file", None, "arguments"),
    ("grep_search", None, "parameters"),
    ("write_to_file", None, "parameters"),
    ("replace_file_content", None, "parameters"),
    ("run_command", "python -m pytest tests/test_module.py -q", "parameters"),
    ("run_command", "python.exe -m pytest -q", "arguments"),
    ("run_command", "git status", "parameters"),
])
def test_allowlisted_project_tool_steps_are_validated_and_accepted(provider_context, name, command, argument_field):
    profile, module, _, workspace = provider_context
    (workspace / "src").mkdir(exist_ok=True)
    arguments = valid_tool_arguments(name, workspace)
    if command is not None:
        arguments["CommandLine"] = command
    client = project_client(profile, workspace)
    process = FakeProcess([
        init_event(workspace, client.native_agent),
        tool_event(name, arguments, argument_field=argument_field),
        result_event("project task complete"),
    ])
    try:
        response, _ = invoke(client, module, process)
    finally:
        client.close()
    assert response.choices[0].message.content == "project task complete"


@pytest.mark.parametrize(("name", "arguments"), [
    ("view_file", {"AbsolutePath": "../outside.txt"}),
    ("write_to_file", {"TargetFile": "C:/outside.txt", "CodeContent": "blocked"}),
    ("write_to_file", {"TargetFile": ".git/config", "CodeContent": "blocked"}),
    ("view_file", {"AbsolutePath": ".env"}),
    ("grep_search", {"Query": "secret", "SearchPath": ".aws"}),
    ("replace_file_content", {"TargetFile": ".codex/auth.json", "TargetContent": "old", "ReplacementContent": "blocked"}),
    ("view_file", {"AbsolutePath": "subdir/.env"}),
    ("view_file", {"AbsolutePath": "config/.env.local"}),
    ("write_to_file", {"TargetFile": "deep/path/.git/config", "CodeContent": "blocked"}),
    ("view_file", {"AbsolutePath": "secrets/token.json"}),
    ("view_file", {"AbsolutePath": "keys/id_rsa"}),
    ("view_file", {"AbsolutePath": "certs/server.pem"}),
])
def test_project_file_tool_rejects_outside_and_sensitive_paths(provider_context, name, arguments):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([
        init_event(workspace, client.native_agent),
        tool_event(name, arguments),
        result_event(),
    ])
    try:
        with pytest.raises((RuntimeError, ValueError, PermissionError), match="(?i)path|workspace|sensitive|protected|denied"):
            invoke(client, module, process)
    finally:
        client.close()


@pytest.mark.parametrize("command", [
    "git commit -am forbidden",
    "git diff",
    "git status --short",
    "git status --porcelain",
    "git -c alias.status=!whoami status",
    "git --git-dir=../other/.git status",
    "git -C .. status",
    "git.exe status",
    "git  status",
    "git\tstatus",
    "git status; whoami",
    "git diff && whoami",
    "git diff --no-index .. outside",
    "git status --git-dir=../other/.git",
    "python -m pytest && whoami",
    "python -m pytest ..",
    "python -m pytest --rootdir=..",
    "python -c print(1)",
    "pwsh -Command Get-ChildItem",
    "curl https://example.invalid",
])
def test_project_command_tool_rejects_non_allowlisted_or_injected_commands(provider_context, command):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([
        init_event(workspace, client.native_agent),
        tool_event("run_command", {"CommandLine": command, "Cwd": str(workspace)}),
        result_event(),
    ])
    try:
        with pytest.raises((RuntimeError, ValueError, PermissionError), match="(?i)command|allow|denied|shell"):
            invoke(client, module, process)
    finally:
        client.close()


@pytest.mark.parametrize("cwd", ["..", ".git", ".aws", ".codex"])
def test_project_command_tool_rejects_outside_or_sensitive_working_directory(provider_context, cwd):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([
        init_event(workspace, client.native_agent),
        tool_event("run_command", {"command": "git status", "cwd": cwd}),
        result_event(),
    ])
    try:
        with pytest.raises((RuntimeError, ValueError, PermissionError), match="(?i)path|workspace|working|protected|denied"):
            invoke(client, module, process)
    finally:
        client.close()


@pytest.mark.parametrize("event", [
    tool_event("delete_file", {"path": "README.md"}),
    tool_event("read_url", {"url": "https://example.invalid"}),
    tool_event("execute_url", {"url": "https://example.invalid"}),
    tool_event("mcp", {"server": "fixture"}),
    tool_event("unsandboxed", {"command": "git status"}),
    tool_event("view_file", {"path": "README.md"}, info_name="write_to_file"),
    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "view_file", "tool_info": {"name": "view_file"}}},
    {"event": "step_update", "step_update": {"step_type": "subagent", "subagent_info": {"name": "helper"}}},
])
def test_unknown_inconsistent_or_incomplete_tool_steps_are_rejected(provider_context, event):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([init_event(workspace, client.native_agent), event, result_event()])
    try:
        with pytest.raises((NotImplementedError, RuntimeError, ValueError), match="(?i)tool|subagent|argument|mismatch|unsupported"):
            invoke(client, module, process)
    finally:
        client.close()


@pytest.mark.parametrize("event", [
    tool_event("view_file", {}),
    tool_event("view_file", {"path": 123}),
    tool_event("write_to_file", {"content": "missing path"}),
    tool_event("replace_file_content", {"old_content": "old", "new_content": "new"}),
])
def test_file_tool_steps_require_a_valid_reported_path(provider_context, event):
    profile, module, _, workspace = provider_context
    client = project_client(profile, workspace)
    process = FakeProcess([init_event(workspace, client.native_agent), event, result_event()])
    try:
        with pytest.raises((RuntimeError, ValueError), match="(?i)path|parameter|tool|file"):
            invoke(client, module, process)
    finally:
        client.close()


def test_configure_can_enable_and_disable_project_tools_only_for_coop_dev(setup_module, tmp_path):
    home = tmp_path / "hermes"
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    setup_module.configure(home)
    unchanged = profile_files(home, "coop-review", "coop-expert")
    root_config = (home / "config.yaml").read_bytes()

    setup_module.configure(
        home,
        profile_name="coop-dev",
        enable_project_tools=True,
        project_workspace=workspace,
    )
    config_path = home / "profiles/coop-dev/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["antigravity_cli"]["project_execution"] == {
        "enabled": True,
        "workspace": str(workspace),
    }
    assert config["antigravity_cli"]["experimental_subscription_bridge"] is True
    assert all(path.read_bytes() == contents for path, contents in unchanged.items())
    assert (home / "config.yaml").read_bytes() == root_config

    setup_module.configure(
        home,
        profile_name="coop-dev",
        enable_project_tools=False,
    )
    disabled = yaml.safe_load(config_path.read_text(encoding="utf-8"))["antigravity_cli"]["project_execution"]
    assert disabled.get("enabled") is False


@pytest.mark.parametrize("profile_name", ["coop-review", "coop-expert"])
def test_configure_refuses_project_tools_for_non_developer_profiles(setup_module, tmp_path, profile_name):
    home = tmp_path / "hermes"
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    setup_module.configure(home)
    before = profile_files(home, "coop-dev", "coop-review", "coop-expert")
    with pytest.raises(ValueError, match="coop-dev|project tools"):
        setup_module.configure(
            home,
            profile_name=profile_name,
            enable_project_tools=True,
            project_workspace=workspace,
        )
    assert all(path.read_bytes() == contents for path, contents in before.items())


@pytest.mark.parametrize("workspace", [Path("relative/project"), None])
def test_configure_requires_absolute_workspace_when_enabling(setup_module, tmp_path, workspace):
    home = tmp_path / "hermes"
    setup_module.configure(home)
    config_path = home / "profiles/coop-dev/config.yaml"
    before = config_path.read_bytes()
    with pytest.raises(ValueError, match="workspace|absolute"):
        setup_module.configure(
            home,
            profile_name="coop-dev",
            enable_project_tools=True,
            project_workspace=workspace,
        )
    assert config_path.read_bytes() == before
