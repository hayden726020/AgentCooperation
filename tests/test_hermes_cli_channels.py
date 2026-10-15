"""Comprehensive regression tests for Antigravity CLI and Codex CLI Hermes Bot channels.

Verifies:
1. Codex app-server protocol:
   - item/agentMessage/delta with string params.delta.
   - item/completed for final text without duplicate concatenation.
   - turn/completed status and error handling.
   - Rejection of empty successful responses.
2. Expert Bot permission & MCP isolation:
   - Dynamic host MCP discovery and explicit disable flags (-c mcp_servers.<name>.enabled=false, --disable plugins).
   - Protocol-compliant declines for commandExecution, fileChange, elicitation, and permissions.
   - Security violation detection if tool execution is attempted.
3. Antigravity result validation & process recovery:
   - JSON arrays and malformed lines handled safely without unhandled exception.
   - Error result with exit code 0 fails properly.
   - Zero-event exit code 0 fails.
   - Bounded exit wait prevents false timeouts on normal exit.
   - Unified cleanup on all error/exit paths.
4. Real-time streaming & early cancellation:
   - Incrementally yields chunks as events arrive.
   - Consumer early break triggers process cleanup and client disposal.
5. Session working directory:
   - Workspaces with spaces.
   - Runtime session cwd binding (agent.runtime_cwd.set_session_cwd).
   - Parallel session isolation.
6. Error redaction & mock transparency:
   - Codex stderr redaction.
   - No ambient env var silently faking mock responses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

HERMES_ROOT = Path(__file__).resolve().parents[1] / "hermes-agent"
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

# Load plugin client modules directly via spec to ensure stable namespace
def _load_module_from_file(rel_path: str, mod_name: str):
    file_path = HERMES_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module

agy_client_mod = _load_module_from_file(
    "plugins/model-providers/antigravity-cli/client.py",
    "hermes_plugin_provider_antigravity_cli_client",
)
codex_client_mod = _load_module_from_file(
    "plugins/model-providers/codex-cli/client.py",
    "hermes_plugin_provider_codex_cli_client",
)

AntigravityCLIClient = agy_client_mod.AntigravityCLIClient
AntigravityConsumerPlanUnsupportedError = agy_client_mod.AntigravityConsumerPlanUnsupportedError
AntigravityAuthError = agy_client_mod.AntigravityAuthError
AntigravityQuotaExhaustedError = agy_client_mod.AntigravityQuotaExhaustedError
DEFAULT_ANTIGRAVITY_MODEL = agy_client_mod.DEFAULT_ANTIGRAVITY_MODEL

CodexCLIClient = codex_client_mod.CodexCLIClient
CodexModelUnavailableError = codex_client_mod.CodexModelUnavailableError
CodexAuthExpiredError = codex_client_mod.CodexAuthExpiredError
CodexQuotaExhaustedError = codex_client_mod.CodexQuotaExhaustedError
CodexSecurityViolationError = codex_client_mod.CodexSecurityViolationError
DEFAULT_CODEX_EXPERT_MODEL = codex_client_mod.DEFAULT_CODEX_EXPERT_MODEL

from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


@pytest.fixture(autouse=True)
def isolated_cli_channel_home(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "channel-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "channel-runtime"))
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


# ============================================================================
# 1. Codex app-server Protocol: String Delta, item/completed, Turn Status
# ============================================================================

def test_codex_real_protocol_string_delta_and_item_completed(tmp_path):
    """Verify Codex parser correctly handles string params.delta and item/completed without duplicating text."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True
    mock_app_client.take_server_request.return_value = None

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_real"}},
        {"turn": {"id": "tu_real"}},
    ]

    notifications = [
        # String delta in real codex app-server
        {"method": "item/agentMessage/delta", "params": {"delta": "Hello "}},
        {"method": "item/agentMessage/delta", "params": {"delta": "from Sol 6.1"}},
        # Completed agentMessage item carrying full text
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Hello from Sol 6.1"}}},
        # Turn completed event with completed status
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notifications.pop(0) if notifications else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        res = client.chat.completions.create(
            messages=[{"role": "user", "content": "Analyze architecture."}],
        )

    # Content must equal the message, not duplicate "Hello from Sol 6.1Hello from Sol 6.1"
    assert res.choices[0].message.content == "Hello from Sol 6.1"
    assert res.model == "gpt-6.1-sol"


def test_codex_turn_failed_status_raises_error(tmp_path):
    """Verify turn/completed with non-completed status and error raises RuntimeError."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True
    mock_app_client.take_server_request.return_value = None

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_fail"}},
        {"turn": {"id": "tu_fail"}},
    ]

    notifications = [
        {"method": "turn/completed", "params": {"turn": {"status": "failed", "error": "Model execution failed"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notifications.pop(0) if notifications else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        with pytest.raises(RuntimeError, match="Codex turn error: Model execution failed"):
            client.chat.completions.create(
                messages=[{"role": "user", "content": "Analyze architecture."}],
            )


def test_codex_empty_response_fails(tmp_path):
    """Verify turn that finishes with no deltas and no final content raises RuntimeError."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True
    mock_app_client.take_server_request.return_value = None

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_empty"}},
        {"turn": {"id": "tu_empty"}},
    ]

    notifications = [
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notifications.pop(0) if notifications else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        with pytest.raises(RuntimeError, match="no valid response content"):
            client.chat.completions.create(
                messages=[{"role": "user", "content": "Analyze architecture."}],
            )


# ============================================================================
# 2. Expert Bot Permission Isolation & Host MCP Disabling
# ============================================================================

def test_codex_disables_host_mcps_and_plugins_in_extra_args(tmp_path):
    """Verify host config MCP servers are discovered and explicitly disabled in CLI extra_args."""
    mock_config = tmp_path / "mock_codex_config.toml"
    mock_config.write_text(
        '[mcp_servers.node_repl]\ncommand = "node_repl.exe"\n\n'
        '[mcp_servers.writable_custom_tool]\ncommand = "tool.exe"\n',
        encoding="utf-8",
    )

    client = CodexCLIClient(cwd=str(tmp_path), config_path=mock_config)
    extra_args = client._build_extra_args()

    assert "-c" in extra_args
    assert 'sandbox_mode="read-only"' in extra_args
    assert 'approval_policy="never"' in extra_args
    assert "--disable" in extra_args
    assert "plugins" in extra_args
    assert "mcp_servers.node_repl.enabled=false" in extra_args
    assert "mcp_servers.writable_custom_tool.enabled=false" in extra_args


def test_codex_protocol_approval_and_elicitation_declined(tmp_path):
    """Verify approval requests are declined according to their specific JSON schemas."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_appr"}},
        {"turn": {"id": "tu_appr"}},
    ]

    server_requests = [
        {"id": "r1", "method": "item/commandExecution/requestApproval", "params": {"command": "rm -rf"}},
        {"id": "r2", "method": "item/fileChange/requestApproval", "params": {"path": "a.py"}},
        {"id": "r3", "method": "mcpServer/elicitation/request", "params": {"serverName": "node_repl"}},
        {"id": "r4", "method": "item/permissions/requestApproval", "params": {"scope": "turn"}},
        {"id": "r5", "method": "mcpServer/tool/call", "params": {"server": "node_repl", "tool": "eval"}},
    ]
    mock_app_client.take_server_request.side_effect = lambda timeout=0: server_requests.pop(0) if server_requests else None

    notifications = [
        {"method": "item/agentMessage/delta", "params": {"delta": "Analysis complete."}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notifications.pop(0) if notifications else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        res = client.chat.completions.create(messages=[{"role": "user", "content": "test"}])

    # Assert specific protocol responses
    mock_app_client.respond.assert_any_call("r1", {"decision": "decline"})
    mock_app_client.respond.assert_any_call("r2", {"decision": "decline"})
    mock_app_client.respond.assert_any_call("r3", {"action": "decline", "content": None, "_meta": None})
    mock_app_client.respond.assert_any_call("r4", {"permissions": {"fileSystem": None, "network": None}, "scope": "turn"})
    mock_app_client.respond_error.assert_called_with("r5", code=-32601, message="Tool call mcpServer/tool/call rejected: expert bot is strictly read-only")
    assert res.choices[0].message.content == "Analysis complete."


def test_codex_rejects_unauthorized_tool_execution(tmp_path):
    """Verify expert bot raises CodexSecurityViolationError if any tool item is initiated."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True
    mock_app_client.take_server_request.return_value = None

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_sec"}},
        {"turn": {"id": "tu_sec"}},
    ]

    notifications = [
        {"method": "item/started", "params": {"item": {"type": "mcpToolCall", "id": "m1"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notifications.pop(0) if notifications else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        with pytest.raises(CodexSecurityViolationError, match="Read-only security violation"):
            client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])


# ============================================================================
# 3. Antigravity Result Validation, Bounded Exit & Process Recovery
# ============================================================================

def test_antigravity_handles_json_arrays_and_malformed_lines(tmp_path, monkeypatch):
    """Verify JSON arrays and malformed lines are safely ignored without unhandled exceptions."""
    monkeypatch.setenv("AGY_TEST_SUBPROCESS", "1")

    class MockRobustPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 99881
            self.stdin = MagicMock()
            lines = [
                "[]\n",  # JSON array line
                "{\"not_an_event\": 123}\n",
                "{\"event\": \"init\", \"init\": {\"model\": \"gemini-3.8-flash-high\"}}\n",
                "{\"event\": \"step_update\", \"step_update\": {\"step_type\": \"agent_response\", \"text_delta\": \"Valid response.\"}}\n",
                "{\"event\": \"result\", \"result\": {\"status\": \"SUCCESS\", \"response\": \"Valid response.\"}}\n",
                "",
            ]
            self.stdout = MagicMock()
            self.stdout.readline.side_effect = lines
            self.stderr = MagicMock()
            self.stderr.readline.side_effect = [""]

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    client = AntigravityCLIClient(cwd=str(tmp_path), mock_mode=True)
    with patch("subprocess.Popen", side_effect=MockRobustPopen):
        res = client.chat.completions.create(messages=[{"role": "user", "content": "test"}])
    assert res.choices[0].message.content == "Valid response."


def test_antigravity_error_result_with_exit_code_zero_raises(tmp_path, monkeypatch):
    """Verify result event with status error raises appropriate exception even if exit code is 0."""
    monkeypatch.setenv("AGY_TEST_SUBPROCESS", "1")

    class MockErrorResultPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 99882
            self.stdin = MagicMock()
            lines = [
                "{\"event\": \"result\", \"result\": {\"status\": \"error\", \"error\": \"User rate limit exhausted.\"}}\n",
                "",
            ]
            self.stdout = MagicMock()
            self.stdout.readline.side_effect = lines
            self.stderr = MagicMock()
            self.stderr.readline.side_effect = [""]

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    client = AntigravityCLIClient(cwd=str(tmp_path), mock_mode=True)
    with patch("subprocess.Popen", side_effect=MockErrorResultPopen):
        with pytest.raises(AntigravityQuotaExhaustedError, match="quota exhausted"):
            client.chat.completions.create(messages=[{"role": "user", "content": "test"}])


def test_antigravity_zero_events_exit_zero_raises(tmp_path, monkeypatch):
    """Verify process exiting code 0 without emitting valid events raises RuntimeError."""
    monkeypatch.setenv("AGY_TEST_SUBPROCESS", "1")

    class MockEmptyPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 99883
            self.stdin = MagicMock()
            self.stdout = MagicMock()
            self.stdout.readline.return_value = ""
            self.stderr = MagicMock()
            self.stderr.readline.return_value = ""

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    client = AntigravityCLIClient(cwd=str(tmp_path), mock_mode=True)
    with patch("subprocess.Popen", side_effect=MockEmptyPopen):
        with pytest.raises(RuntimeError, match="without emitting any valid response"):
            client.chat.completions.create(messages=[{"role": "user", "content": "test"}])


def test_antigravity_bounded_exit_wait(tmp_path, monkeypatch):
    """Verify normal exit is given bounded wait and not falsely terminated as timeout."""
    monkeypatch.setenv("AGY_TEST_SUBPROCESS", "1")

    class MockDelayedExitPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 99884
            self.stdin = MagicMock()
            lines = [
                "{\"event\": \"init\", \"init\": {\"model\": \"gemini-3.8-flash-high\"}}\n",
                "{\"event\": \"result\", \"result\": {\"status\": \"SUCCESS\", \"response\": \"Done successfully.\"}}\n",
                "",
            ]
            self.stdout = MagicMock()
            self.stdout.readline.side_effect = lines
            self.stderr = MagicMock()
            self.stderr.readline.side_effect = [""]
            self._waited = False

        def poll(self):
            # Returns None initially, 0 after wait
            return 0 if self._waited else None

        def wait(self, timeout=None):
            self._waited = True
            return 0

    client = AntigravityCLIClient(cwd=str(tmp_path), mock_mode=True)
    with patch("subprocess.Popen", side_effect=MockDelayedExitPopen):
        res = client.chat.completions.create(messages=[{"role": "user", "content": "test"}])
    assert res.choices[0].message.content == "Done successfully."


# ============================================================================
# 4. Real-Time Streaming and Early Cancellation
# ============================================================================

def test_antigravity_realtime_streaming_and_early_cancellation(tmp_path, monkeypatch):
    """Verify tokens are yielded incrementally and early consumer exit cleans up process."""
    monkeypatch.setenv("AGY_TEST_SUBPROCESS", "1")
    inbox_queue = queue.Queue()

    class MockStreamPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 99885
            self.stdin = MagicMock()
            self.stdout = MagicMock()
            self.stdout.readline.side_effect = [
                "{\"event\": \"init\", \"init\": {\"model\": \"gemini-3.8-flash-high\"}}\n",
                "{\"event\": \"step_update\", \"step_update\": {\"step_type\": \"agent_response\", \"text_delta\": \"First token \"}}\n",
                "{\"event\": \"step_update\", \"step_update\": {\"step_type\": \"agent_response\", \"text_delta\": \"Second token\"}}\n",
                "{\"event\": \"result\", \"result\": {\"status\": \"SUCCESS\", \"response\": \"First token Second token\"}}\n",
                "",
            ]
            self.stderr = MagicMock()
            self.stderr.readline.return_value = ""

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    client = AntigravityCLIClient(cwd=str(tmp_path), mock_mode=True)
    with patch("subprocess.Popen", side_effect=MockStreamPopen) as mock_p:
        with patch.object(agy_client_mod, "kill_process_tree") as mock_kill:
            stream = client.chat.completions.create(
                messages=[{"role": "user", "content": "Stream test"}],
                stream=True,
            )
            # Consumer receives first chunk
            first_chunk = next(stream)
            assert first_chunk.choices[0].delta.content == "First token "

            # Early consumer exit / close
            stream.close()
            mock_kill.assert_called_with(99885)
            assert len(client._active_processes) == 0


def test_codex_realtime_streaming_and_early_cancellation(tmp_path):
    """Verify Codex yields deltas as notifications arrive and closing generator reaps client."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = True
    mock_app_client.take_server_request.return_value = None

    mock_app_client.request.side_effect = [
        {"thread": {"id": "th_st"}},
        {"turn": {"id": "tu_st"}},
    ]

    notes = [
        {"method": "item/agentMessage/delta", "params": {"delta": "First "}},
        {"method": "item/agentMessage/delta", "params": {"delta": "Second"}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    mock_app_client.take_notification.side_effect = lambda timeout=0: notes.pop(0) if notes else None

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        stream = client.chat.completions.create(messages=[{"role": "user", "content": "hi"}], stream=True)

        chunk1 = next(stream)
        assert chunk1.choices[0].delta.content == "First "

        # Early break
        stream.close()
        mock_app_client.close.assert_called_once()
        assert len(client._active_clients) == 0


# ============================================================================
# 5. Session Working Directory (Spaces & Runtime Binding)
# ============================================================================

def test_session_cwd_with_spaces_and_pinning(tmp_path):
    """Verify cwd containing spaces is properly resolved and isolated across parallel clients."""
    space_dir = tmp_path / "My Workspace With Spaces"
    space_dir.mkdir()

    from agent.runtime_cwd import set_session_cwd, reset_session_cwd
    token = set_session_cwd(str(space_dir))
    try:
        c1 = CodexCLIClient()
        assert Path(c1.cwd).resolve() == space_dir.resolve()

        c2 = AntigravityCLIClient()
        assert Path(c2.cwd).resolve() == space_dir.resolve()

        # Parallel client with custom cwd maintains distinct directory
        other_dir = tmp_path / "Other Workspace"
        other_dir.mkdir()
        c3 = CodexCLIClient(cwd=str(other_dir))
        assert Path(c3.cwd).resolve() == other_dir.resolve()
        assert Path(c1.cwd).resolve() != Path(c3.cwd).resolve()
    finally:
        reset_session_cwd(token)


# ============================================================================
# 6. Error Redaction & Transparent Mock Mode
# ============================================================================

def test_codex_redacts_sensitive_stderr(tmp_path):
    """Verify credentials and secret tokens in stderr are redacted before raising exceptions."""
    mock_app_client = MagicMock()
    mock_app_client.is_alive.return_value = False
    mock_app_client.take_server_request.return_value = None
    mock_app_client.take_notification.return_value = None
    mock_app_client.request.return_value = {"thread": {"id": "th_1"}, "turn": {"id": "tu_1"}}
    # Sensitive credential token in stderr
    mock_app_client.stderr_tail.return_value = [
        "Authentication failure for token sk-proj-supersecrettoken123456789. Please re-authenticate."
    ]

    with patch.object(codex_client_mod, "CodexAppServerClient", return_value=mock_app_client):
        client = CodexCLIClient(cwd=str(tmp_path))
        with pytest.raises(CodexAuthExpiredError) as exc_info:
            client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])
        assert "supersecrettoken" not in str(exc_info.value)
        assert "..." in str(exc_info.value)


def test_no_implicit_mock_switch_by_ambient_env(tmp_path, monkeypatch):
    """Verify ambient environment variables do NOT silently switch client to mock mode."""
    monkeypatch.setenv("CODEX_TEST_MODE", "1")
    monkeypatch.setenv("AGY_TEST_MODE", "1")

    c_codex = CodexCLIClient(cwd=str(tmp_path))
    assert c_codex.mock_mode is False

    c_agy = AntigravityCLIClient(cwd=str(tmp_path))
    assert c_agy.mock_mode is False


# ============================================================================
# 7. Model Policy, Provider Resolution & Setup Invariants
# ============================================================================

def test_antigravity_rejects_model_switch():
    """Verify agy client refuses switching to any other model."""
    client = AntigravityCLIClient(mock_mode=True)
    with pytest.raises(ValueError, match="fixed to 'gemini-3.8-flash-high'"):
        client.chat.completions.create(
            model="gemini-2.5-pro",
            messages=[{"role": "user", "content": "hi"}],
        )


def test_codex_fixed_sol_model_enforcement():
    """Verify Codex expert refuses switching away from gpt-6.1-sol."""
    client = CodexCLIClient(mock_mode=True)
    res = client.chat.completions.create(
        messages=[{"role": "user", "content": "Analyze architecture."}],
    )
    assert res.model == "gpt-6.1-sol"

    with pytest.raises(CodexModelUnavailableError, match="fixed to 'gpt-6.1-sol'"):
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Analyze architecture."}],
        )


def test_provider_resolution_and_gemini_api_disabled(tmp_path):
    """Verify antigravity-cli and codex-cli resolve cleanly, and native Gemini is blocked."""
    agy_prov = resolve_runtime_provider(requested="antigravity-cli")
    assert agy_prov["source"] == "process"
    assert get_provider_profile("antigravity-cli").auth_type == "external_process"

    with patch("shutil.which", side_effect=lambda cmd, *args, **kwargs: "C:/Tools/codex.exe" if "codex" in str(cmd) else None):
        codex_prov = resolve_runtime_provider(requested="codex-cli")
        assert codex_prov["source"] == "process"
        assert get_provider_profile("codex-cli").auth_type == "external_process"

    from scripts.configure_hermes_bots import configure, hermes_home
    home = tmp_path / "hermes_test_home"
    configure(home)

    with hermes_home(home / "profiles/coop-dev"):
        with pytest.raises(ValueError, match="disabled"):
            resolve_runtime_provider(requested="gemini")


def test_antigravity_policy_guard_blocks_unauthorized_consumer_plan():
    """Verify policy check prevents live calls on consumer plans per Google FAQ."""
    client = AntigravityCLIClient(mock_mode=False)
    with pytest.raises(AntigravityConsumerPlanUnsupportedError, match="consumer plan does not support wrapping agy"):
        client.chat.completions.create(
            messages=[{"role": "user", "content": "test live"}],
        )
