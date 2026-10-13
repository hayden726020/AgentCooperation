"""CLI proxy inheritance stays explicit and never reaches generated tests."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution import proxy


def test_disabled_windows_proxy_and_pac_are_not_executed(monkeypatch):
    monkeypatch.setattr(proxy, "_read_windows_proxy", lambda: {
        "ProxyEnable": 0, "ProxyServer": "127.0.0.1:7890", "AutoConfigURL": "https://fixture/pac.js",
    })
    assert proxy.get_cli_proxy_env({}) == {}


@pytest.mark.parametrize("server,expected", [
    ("127.0.0.1:7890", {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890"}),
    ("http=first:80;https=second:443", {"HTTP_PROXY": "http://first:80", "HTTPS_PROXY": "http://second:443"}),
    ("http=first:80;socks=third:1080", {"HTTP_PROXY": "http://first:80", "ALL_PROXY": "socks5://third:1080"}),
])
def test_manual_windows_proxy_mapping(monkeypatch, server, expected):
    monkeypatch.setattr(proxy, "_read_windows_proxy", lambda: {"ProxyEnable": 1, "ProxyServer": server})
    assert proxy.get_cli_proxy_env({}) == expected


def test_explicit_environment_is_authoritative_and_case_insensitive(monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(proxy, "_read_windows_proxy", reader)
    expected = {"HTTP_PROXY": "http://explicit:8080", "HTTPS_PROXY": "https://secure:8443",
                "ALL_PROXY": "socks5://fallback:1080", "NO_PROXY": "localhost,fixture"}
    assert proxy.get_cli_proxy_env({key.lower(): value for key, value in expected.items()}) == expected
    reader.assert_not_called()


def test_explicit_empty_proxy_disables_registry_inheritance(monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(proxy, "_read_windows_proxy", reader)
    assert proxy.get_cli_proxy_env({"https_proxy": ""}) == {"HTTPS_PROXY": ""}
    reader.assert_not_called()


def test_no_proxy_preserved_with_registry_transport(monkeypatch):
    monkeypatch.setattr(proxy, "_read_windows_proxy", lambda: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890"})
    result = proxy.get_cli_proxy_env({"no_proxy": "localhost,127.0.0.1"})
    assert result["NO_PROXY"] == "localhost,127.0.0.1"
    assert result["HTTPS_PROXY"] == "http://127.0.0.1:7890"


@pytest.mark.parametrize("value", ["file:///credentials", "http://host:bad", "http://host:99999", "http://host/path", "http://user:secret@host\nINJECT=1"])
def test_invalid_proxy_fails_without_echoing_credentials(value):
    with pytest.raises(ValueError) as caught:
        proxy.get_cli_proxy_env({"HTTPS_PROXY": value})
    assert value not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_proxy_authentication_redacted_from_diagnostics():
    url = "http://fixture-user:fixture-password@localhost:7890"
    output = f"Could not connect to {url}; password fixture-password"
    safe = proxy.redact_cli_proxy_credentials(output, {"HTTPS_PROXY": url})
    assert "fixture-password" not in safe
    assert "fixture-user" not in safe


def test_encoded_proxy_password_redacted_when_echoed_separately():
    url = "http://fixture-user:p%40ssword@localhost:7890"
    safe = proxy.redact_cli_proxy_credentials(f"Proxy {url}; failed p@ssword", {"HTTPS_PROXY": url})
    assert "p@ssword" not in safe
    assert "p%40ssword" not in safe


@pytest.mark.parametrize("echoed", ["Fixture%2fSecret%3aWord", "Fixture%2FSecret%3aWord"])
def test_encoded_proxy_password_redacts_percent_hex_case_variants(echoed):
    url = "http://fixture-user:Fixture%2FSecret%3AWord@localhost:7890"
    safe = proxy.redact_cli_proxy_credentials(f"Authentication failed; password {echoed}", {"HTTPS_PROXY": url})
    assert echoed not in safe
    assert "[redacted]" in safe


def test_proxy_password_redaction_keeps_ordinary_characters_case_sensitive():
    url = "http://fixture-user:Fixture%2FSecret@localhost:7890"
    message = "Unrelated value fixture%2fsecret; password Fixture%2fSecret"
    safe = proxy.redact_cli_proxy_credentials(message, {"HTTPS_PROXY": url})
    assert "fixture%2fsecret" in safe
    assert "Fixture%2fSecret" not in safe


def test_windows_registry_is_read_without_auto_configuration(monkeypatch):
    values = {"ProxyEnable": 1, "ProxyServer": "localhost:7890"}
    queried = []
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def query(key, name):
        queried.append(name)
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    fake = SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *args: Key(), QueryValueEx=query)
    monkeypatch.setattr(proxy.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert proxy._read_windows_proxy() == values
    assert "AutoConfigURL" not in queried


@pytest.mark.asyncio
async def test_generated_test_process_never_inherits_proxy_credentials(monkeypatch):
    from execution.subproc import run_async_command
    monkeypatch.setenv("HTTPS_PROXY", "http://fixture-user:fixture-password@localhost:7890")
    result = await run_async_command([sys.executable, "-c", "import os; print(any('proxy' in key.lower() for key in os.environ))"])
    assert result.is_success
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("module,class_name", [
    ("agents.gemini_reviewer", "GeminiReviewer"),
    ("agents.codex_expert", "CodexExpert"),
    ("agents.antigravity_reviewer", "AntigravityReviewer"),
])
@pytest.mark.asyncio
async def test_cli_agents_receive_proxy_and_redact_errors(monkeypatch, tmp_path, module, class_name):
    import importlib
    from execution.subproc import SubprocResult
    adapter = importlib.import_module(module)
    url = "http://fixture-user:fixture-password@localhost:7890"
    monkeypatch.setattr(adapter, "get_cli_proxy_env", lambda: {"HTTPS_PROXY": url})
    command = AsyncMock(return_value=SubprocResult(["fixture"], 1, "", f"Failed {url} fixture-password", 0))
    monkeypatch.setattr(adapter, "run_async_command", command)
    if class_name == "AntigravityReviewer":
        monkeypatch.setattr(adapter.CodexExpert, "_verify_expected_head", AsyncMock())
    result = await getattr(adapter, class_name)().execute("fixture", {
        "worktree_path": tmp_path, "expected_commit_sha": "a" * 40 if class_name == "AntigravityReviewer" else None,
    })
    assert not result.success
    assert command.call_args.kwargs["env"]["HTTPS_PROXY"] == url
    assert "fixture-user" not in result.error
    assert "fixture-password" not in result.error
    if class_name == "AntigravityReviewer":
        assert command.call_args.kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
