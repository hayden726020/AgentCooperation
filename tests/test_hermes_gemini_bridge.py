"""Offline contracts for Hermes' explicitly enabled subscription CLI bridge."""

from __future__ import annotations

import io
import json
import queue
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

HERMES_ROOT = Path(__file__).resolve().parents[1] / "hermes-agent"
sys.path.insert(0, str(HERMES_ROOT)) if str(HERMES_ROOT) not in sys.path else None

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from providers import get_provider_profile
from hermes_platform.host.facts import os_family


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    profile_home = tmp_path / "hermes"
    profile_home.mkdir()
    native_home = tmp_path / "native"
    settings = native_home / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"modelProvider":"antigravity"}', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(native_home))
    monkeypatch.setenv("HOME", str(native_home))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    token = set_hermes_home_override(profile_home)
    profile = get_provider_profile("antigravity-cli")
    client = profile.create_client(cwd=str(tmp_path), subscription_bridge_enabled=True)
    module = sys.modules[client.__class__.__module__]
    yield profile, client, module, profile_home, settings
    client.close()
    reset_hermes_home_override(token)


class InputPipe(io.StringIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, events, *, exit_code=0, prepend_init=True):
        self.pid = 900123
        self.stdin = InputPipe()
        if prepend_init and not any(event.get("event") == "init" for event in events):
            events = [{"event": "init", "init": {"model": "gemini-3.8-flash-high"}}, *events]
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
        self.stderr = io.StringIO("")
        self.returncode = None
        self.exit_code = exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert self.stdin.closed, "CLI requires input EOF to complete its turn"
        self.returncode = self.exit_code
        return self.returncode


def result(response="hello", status="SUCCESS"):
    return {"event": "result", "result": {"status": status, "response": response}}


def call_with_process(client, module, process, **kwargs):
    with patch.object(module.subprocess, "Popen", return_value=process) as spawn:
        with patch.object(module, "kill_process_tree", side_effect=lambda _: setattr(process, "returncode", -1)):
            value = client.chat.completions.create(messages=[{"role": "user", "content": "hello"}], **kwargs)
            if kwargs.get("stream"):
                value = list(value)
    return value, spawn


def test_profile_opt_in_is_strict_and_scoped(bridge):
    profile, _, _, home, _ = bridge
    for value in [None, False, "true", "false", 1]:
        (home / "config.yaml").write_text(yaml.safe_dump({"antigravity_cli": {"experimental_subscription_bridge": value}}), encoding="utf-8")
        assert profile.create_client().subscription_bridge_enabled is False
    (home / "config.yaml").write_text(yaml.safe_dump({"antigravity_cli": {"experimental_subscription_bridge": True}}), encoding="utf-8")
    assert profile.create_client().subscription_bridge_enabled is True
    assert profile.create_client(subscription_bridge_enabled=False).subscription_bridge_enabled is False


def test_default_block_does_not_trust_enterprise_env(bridge, monkeypatch):
    profile, _, module, _, _ = bridge
    monkeypatch.setenv("ANTIGRAVITY_ENTERPRISE_AUTHORIZED", "1")
    with patch.object(module.subprocess, "Popen") as spawn:
        with pytest.raises(module.AntigravityConsumerPlanUnsupportedError):
            profile.create_client().chat.completions.create(messages=[{"role": "user", "content": "hi"}])
        spawn.assert_not_called()


def test_stdin_eof_complete_history_and_success_cleanup(bridge):
    _, client, module, _, _ = bridge
    proc = FakeProcess([result()])
    with patch.object(module.subprocess, "Popen", return_value=proc):
        response = client.chat.completions.create(messages=[{"role": "system", "content": "system"}, {"role": "assistant", "content": "previous"}, {"role": "user", "content": "next"}])
    assert response.choices[0].message.content == "hello"
    payload = json.loads(proc.stdin.sent)
    assert payload["event"] == "user"
    assert all(text in payload["message"]["content"] for text in ("system", "previous", "next"))
    assert proc.stdout.closed and proc.stderr.closed
    assert not client._active_processes


@pytest.mark.parametrize("events", [
    [{"event": "init", "init": {"model": "gemini-3.8-flash-high"}}],
    [result("", "SUCCESS")],
    [result("hello", "ERROR")],
    [result("hello", "error")],
    [{"event": "result", "result": {"response": "hello"}}],
])
@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_or_unsuccessful_turn_is_not_success(bridge, events, stream):
    _, client, module, _, _ = bridge
    proc = FakeProcess(events)
    with pytest.raises(RuntimeError):
        call_with_process(client, module, proc, stream=stream)
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed
    assert not client._active_processes


def test_only_agent_response_text_and_final_fallback_are_streamed(bridge):
    _, client, module, _, _ = bridge
    events = [{"event": "step_update", "step_update": {"step_type": kind, "text_delta": text}} for kind, text in [("user_input", "prompt"), ("reasoning", "thought"), ("agent_response", "answer")]]
    chunks, _ = call_with_process(client, module, FakeProcess([*events, result("answer")]), stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "answer"
    chunks, _ = call_with_process(client, module, FakeProcess([result("final only")]), stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "final only"


def test_nonempty_hermes_tools_are_explicitly_unsupported(bridge):
    _, client, module, _, _ = bridge
    with patch.object(module.subprocess, "Popen") as spawn:
        with pytest.raises(NotImplementedError, match="tools"):
            client.chat.completions.create(messages=[], tools=[{"type": "function"}])
        spawn.assert_not_called()


def test_spawn_env_drops_api_keys_and_preserves_native_home(bridge, monkeypatch):
    _, client, module, _, settings = bridge
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
        monkeypatch.setenv(key, "offline-test-secret")
    _, spawn = call_with_process(client, module, FakeProcess([result()]))
    env = spawn.call_args.kwargs["env"]
    assert not any(key.upper() in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"} for key in env)
    assert Path(env["USERPROFILE"]) != settings.parents[2]
    assert Path(env["HOME"]) == Path(env["USERPROFILE"])
    assert settings.read_text(encoding="utf-8") == '{"modelProvider":"antigravity"}'


@pytest.mark.parametrize("provider", ["gemini", "unknown", 123])
def test_native_api_provider_settings_block_before_spawn(bridge, provider):
    _, client, module, _, settings = bridge
    settings.write_text(json.dumps({"modelProvider": provider}), encoding="utf-8")
    with patch.object(module.subprocess, "Popen") as spawn:
        with pytest.raises(RuntimeError, match="provider"):
            client.chat.completions.create(messages=[])
        spawn.assert_not_called()


def test_closed_client_cannot_launch(bridge):
    _, client, module, _, _ = bridge
    client.close()
    with patch.object(module.subprocess, "Popen") as spawn:
        with pytest.raises(RuntimeError, match="closed"):
            client.chat.completions.create(messages=[])
        spawn.assert_not_called()


def test_profile_native_agent_is_passed_as_a_single_literal_argument(bridge):
    profile, _, module, home, _ = bridge
    (home / "config.yaml").write_text(yaml.safe_dump({"antigravity_cli": {"experimental_subscription_bridge": True, "native_agent": "hermes-probe"}}), encoding="utf-8")
    client = profile.create_client(cwd=str(home))
    _, spawn = call_with_process(client, module, FakeProcess([result()]))
    command = spawn.call_args.args[0]
    assert command[command.index("--agent") + 1] == "hermes-probe"
    for invalid in ("--danger", "name --flag", "../agent", False):
        with pytest.raises(ValueError, match="literal CLI agent name"):
            profile.create_client(native_agent=invalid)


def test_profile_loader_obeys_effective_managed_configuration(bridge):
    profile, _, _, home, _ = bridge
    (home / "config.yaml").write_text(yaml.safe_dump({"antigravity_cli": {"experimental_subscription_bridge": True}}), encoding="utf-8")
    with patch("hermes_cli.config.load_config_readonly", return_value={"antigravity_cli": {"experimental_subscription_bridge": False}}):
        assert profile.create_client().subscription_bridge_enabled is False


def test_stream_yields_before_final_event_and_closing_reaps_process(bridge):
    _, client, module, _, _ = bridge

    class BlockingPipe:
        def __init__(self):
            self.events = queue.Queue()
            self.events.put(json.dumps({"event": "init", "init": {}}) + "\n")
            self.events.put(json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "first"}}) + "\n")
            self.waiting = threading.Event()
            self.closed = False

        def readline(self):
            self.waiting.set()
            return self.events.get(timeout=5)

        def close(self):
            self.closed = True

    process = FakeProcess([])
    process.stdout = BlockingPipe()

    def terminate(_):
        process.returncode = -1
        process.stdout.events.put("")

    with patch.object(module.subprocess, "Popen", return_value=process):
        with patch.object(module, "kill_process_tree", side_effect=terminate) as kill:
            stream = client.chat.completions.create(messages=[{"role": "user", "content": "hello"}], stream=True)
            first = next(stream)
            assert first.choices[0].delta.content == "first"
            assert process.returncode is None
            stream.close()
            kill.assert_called_once_with(process.pid)
    assert process.stdout.closed and process.stderr.closed and not client._active_processes


def test_native_tool_execution_is_not_faked_as_hermes_tool_call(bridge):
    _, client, module, _, _ = bridge
    process = FakeProcess([{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "tool output", "tool_info": {"name": "read_file"}}}, result()])
    with pytest.raises(NotImplementedError, match="Native CLI tools"):
        call_with_process(client, module, process)
    assert process.stdout.closed and not client._active_processes


@pytest.mark.parametrize("stream", [False, True])
def test_partial_delta_is_completed_using_the_final_response(bridge, stream):
    _, client, module, _, _ = bridge
    process = FakeProcess([{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "hel"}}, result("hello")])
    response, _ = call_with_process(client, module, process, stream=stream)
    text = "".join(chunk.choices[0].delta.content or "" for chunk in response) if stream else response.choices[0].message.content
    assert text == "hello"


@pytest.mark.parametrize("events", [
    [result(), result("later", "ERROR")],
    [result(), result("later", "SUCCESS")],
    [{"event": "init", "init": {}}, {"event": "init", "init": {}}, result()],
    [{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "different"}}, result("hello")],
])
@pytest.mark.parametrize("stream", [False, True])
def test_ambiguous_or_inconsistent_stream_fails(bridge, events, stream):
    _, client, module, _, _ = bridge
    process = FakeProcess(events)
    with pytest.raises(RuntimeError):
        call_with_process(client, module, process, stream=stream)
    assert process.stdout.closed and not client._active_processes


@pytest.mark.parametrize("stream", [False, True])
def test_missing_init_fails_even_with_success_result(bridge, stream):
    _, client, module, _, _ = bridge
    with pytest.raises(RuntimeError, match="initialization"):
        call_with_process(client, module, FakeProcess([result()], prepend_init=False), stream=stream)


@pytest.mark.parametrize("initialization", [{"model": "wrong-model"}, {"cwd": "Z:/wrong"}, {"agent": "wrong-agent"}])
def test_reported_initialization_must_match_requested_context(bridge, initialization):
    _, client, module, _, _ = bridge
    client.native_agent = "hermes-probe"
    with pytest.raises(ValueError, match="mismatch"):
        call_with_process(client, module, FakeProcess([{"event": "init", "init": initialization}, result()]))


@pytest.mark.parametrize("step", [{"step_type": "tool", "text_delta": "tool output"}, {"step_type": "checkpoint", "tool_name": "command"}])
def test_native_tool_type_or_name_cannot_be_silently_ignored(bridge, step):
    _, client, module, _, _ = bridge
    with pytest.raises(NotImplementedError, match="Native CLI tools"):
        call_with_process(client, module, FakeProcess([{"event": "step_update", "step_update": step}, result()]))


@pytest.mark.skipif(os_family() != "win32", reason="Windows manual proxy inheritance")
def test_windows_existing_manual_proxy_is_inherited_without_api_keys(bridge, monkeypatch):
    _, client, module, _, _ = bridge
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    with patch.object(module.urllib.request, "getproxies", return_value={"http": "http://localhost:7890", "https": "http://localhost:7890", "no": "localhost;127.0.0.1;<local>"}):
        _, spawn = call_with_process(client, module, FakeProcess([result()]))
    env = spawn.call_args.kwargs["env"]
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == "http://localhost:7890"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_explicit_empty_proxy_prevents_system_proxy_inheritance(bridge, monkeypatch):
    _, client, module, _, _ = bridge
    monkeypatch.setenv("HTTPS_PROXY", "")
    with patch.object(module.urllib.request, "getproxies") as lookup:
        _, spawn = call_with_process(client, module, FakeProcess([result()]))
        lookup.assert_not_called()
    assert spawn.call_args.kwargs["env"]["HTTPS_PROXY"] == ""


@pytest.mark.skipif(os_family() != "win32", reason="Windows manual proxy inheritance")
def test_invalid_inherited_proxy_fails_without_echoing_credentials(bridge, monkeypatch):
    _, client, module, _, _ = bridge
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    with patch.object(module.urllib.request, "getproxies", return_value={"https": "file://user:private-secret@host/path"}):
        with pytest.raises(ValueError) as caught:
            client.chat.completions.create(messages=[])
        assert "private-secret" not in str(caught.value)


def test_each_request_has_an_isolated_native_home_and_deny_all_workspace(bridge):
    _, client, module, profile_home, original_settings = bridge
    _, first_spawn = call_with_process(client, module, FakeProcess([result()]))
    _, second_spawn = call_with_process(client, module, FakeProcess([result()]))
    first = first_spawn.call_args.kwargs
    second = second_spawn.call_args.kwargs
    assert first["cwd"] != second["cwd"]
    assert Path(first["cwd"]) != Path(client.cwd)
    assert Path(first["cwd"]).is_relative_to(profile_home)
    for spawn in (first_spawn, second_spawn):
        native_home = Path(spawn.call_args.kwargs["env"]["USERPROFILE"])
        config = json.loads((native_home / ".gemini/antigravity-cli/settings.json").read_text(encoding="utf-8"))
        assert config["permissions"]["allow"] == config["permissions"]["ask"] == []
        assert "command(*)" in config["permissions"]["deny"]
        assert "mcp(*)" in config["permissions"]["deny"]
        assert config["useG1Credits"] is False
        assert not (native_home / ".gemini/oauth_creds.json").exists()
        command = spawn.call_args.args[0]
        name = command[command.index("--agent") + 1]
        agent = Path(spawn.call_args.kwargs["cwd"]) / ".agents/agents" / f"{name}.md"
        assert "tools: []" in agent.read_text(encoding="utf-8")
        assert "commandExecutionPolicy: 'off'" in agent.read_text(encoding="utf-8")
    assert original_settings.read_text(encoding="utf-8") == '{"modelProvider":"antigravity"}'


@pytest.mark.skipif(os_family() != "win32", reason="Windows manual proxy inheritance")
def test_explicit_no_proxy_preserved_with_existing_manual_transport(bridge, monkeypatch):
    _, client, module, _, _ = bridge
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,fixture")
    with patch.object(module.urllib.request, "getproxies", return_value={"no": "localhost,fixture"}):
        with patch.object(module.urllib.request, "getproxies_registry", return_value={"https": "http://localhost:7890"}):
            _, spawn = call_with_process(client, module, FakeProcess([result()]))
    assert spawn.call_args.kwargs["env"]["NO_PROXY"] == "localhost,fixture"
    assert spawn.call_args.kwargs["env"]["HTTPS_PROXY"] == "http://localhost:7890"


@pytest.mark.skipif(os_family() != "win32", reason="Windows manual proxy inheritance")
def test_proxy_credentials_are_redacted_even_when_echoed_separately(bridge, monkeypatch):
    _, client, module, _, _ = bridge
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    event = result("", "ERROR")
    event["result"]["error"] = "Proxy failed using private-password at http://fixture-user:private-password@localhost:7890"
    with patch.object(module.urllib.request, "getproxies", return_value={"https": "http://fixture-user:private-password@localhost:7890"}):
        with pytest.raises(RuntimeError) as caught:
            call_with_process(client, module, FakeProcess([event]))
    assert "private-password" not in str(caught.value)
    assert "fixture-user" not in str(caught.value)
