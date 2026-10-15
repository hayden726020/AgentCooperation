"""Offline end-to-end probe checks through the real Hermes provider and agent.

Each check uses a fresh interpreter so Hermes imports cannot bind another test's
or the user's configuration. Only the final native CLI response is replaced.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CHILD_CHECK = r'''
import importlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

project, scratch, output, fail_response = sys.argv[1:]
sys.path.insert(0, project)
sys.path.insert(0, str(Path(project) / "hermes-agent"))

# Environment isolation is already applied before any Hermes/config imports.
from scripts import probe_hermes_gemini as probe_module
from providers import get_provider_profile
from run_agent import AIAgent
import execution.proxy
import yaml

probe_module.PROJECT_ROOT = Path(scratch)
profile = get_provider_profile("antigravity-cli")
assert profile is not None, "Bundled provider discovery must resolve the real profile"
client_module = importlib.import_module(profile.__class__.__module__ + ".client")
client_type = client_module.AntigravityCLIClient

tracked_names = (
    "HERMES_HOME", "HERMES_RUNTIME_DIR", "USERPROFILE", "HOME",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)
before = {name: os.environ.get(name) for name in tracked_names}
calls = []
agents = []
clients = []
scratch_handlers = []
original_init = AIAgent.__init__

def instrument_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    agents.append(self)
    clients.append(self.client)
    assert type(self.client) is client_type
    assert self.client.subscription_bridge_enabled is True
    assert self.client.mock_mode is False, "Only the transport boundary is simulated"
    assert self._api_max_retries == 1
    assert self._auto_recovery_cycles == 0
    # Hermes may install its global logging on initial import. Add actual
    # probe-local handlers on both root and named loggers to exercise cleanup
    # even when that process-global initialization has already happened.
    log_home = Path(os.environ["HERMES_HOME"]) / "logs"
    log_home.mkdir(exist_ok=True)
    for logger, filename in ((logging.getLogger(), "root-test.log"),
                             (logging.getLogger("probe_named_test"), "named-test.log")):
        handler = logging.FileHandler(log_home / filename, encoding="utf-8")
        logger.addHandler(handler)
    for logger in [logging.getLogger()] + [
        entry for entry in logging.Logger.manager.loggerDict.values()
        if isinstance(entry, logging.Logger)
    ]:
        for handler in logger.handlers:
            filename = getattr(handler, "baseFilename", None)
            if filename and Path(filename).resolve().is_relative_to(Path(scratch).resolve()):
                scratch_handlers.append(handler)

def fake_response(self, prompt, model, timeout):
    calls.append((self, prompt, model, timeout))
    assert probe_module.MARKER in prompt
    assert model == probe_module.MODEL
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
        assert key not in os.environ, "The probe must not use API credentials"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:43210"
    runtime_home = Path(os.environ["HERMES_HOME"])
    assert runtime_home.is_relative_to(Path(scratch))
    assert Path(self.cwd).resolve() == runtime_home.parent / "workspace"
    assert not self._active_processes
    if fail_response == "yes":
        raise RuntimeError("Offline simulated authentication failure")
    yield client_module._make_stream_chunk(probe_module.MARKER, model)
    yield client_module._make_stream_chunk("", model, "stop")

def forbidden(*args, **kwargs):
    raise AssertionError("Offline probe test attempted a native process or network connection")

# This caller-owned logger must remain usable after the probe cleans its logs.
user_log = Path(output).with_suffix(".caller.log")
caller_handler = logging.FileHandler(user_log, encoding="utf-8")
caller_logger = logging.getLogger("probe_caller_test")
caller_logger.addHandler(caller_handler)
caller_logger.setLevel(logging.INFO)
try:
    with patch.object(client_type, "_stream_response", fake_response), \
         patch.object(AIAgent, "__init__", instrument_init), \
         patch.object(execution.proxy, "get_cli_proxy_env", return_value={
             "HTTP_PROXY": "http://127.0.0.1:43210",
             "HTTPS_PROXY": "http://127.0.0.1:43210",
             "NO_PROXY": "offline-test.invalid",
         }), \
         patch.object(subprocess, "Popen", forbidden), \
         patch.object(socket.socket, "connect", forbidden), \
         patch.object(socket.socket, "connect_ex", forbidden):
        result = probe_module.probe(timeout=3)

    assert {name: os.environ.get(name) for name in tracked_names} == before
    assert len(calls) == 1, "Probe must not make recovery or auxiliary model calls"
    assert len(agents) == 1 and clients[0].is_closed
    assert caller_handler in caller_logger.handlers
    assert caller_handler.stream is not None and not caller_handler.stream.closed
    caller_logger.info("caller logger still usable")
    caller_handler.flush()
    assert "caller logger still usable" in user_log.read_text(encoding="utf-8")

    # Every real scratch file handler must be removed and closed, including
    # handlers registered on named loggers.
    assert len(scratch_handlers) >= 2
    live_handlers = [handler for logger in [logging.getLogger()] + [
        entry for entry in logging.Logger.manager.loggerDict.values()
        if isinstance(entry, logging.Logger)
    ] for handler in logger.handlers]
    assert all(handler not in live_handlers and handler.stream is None for handler in scratch_handlers)

    runs = list((Path(scratch) / ".integration/hermes-gemini/probes").glob("probe-*"))
    assert len(runs) == 1 and runs[0].is_dir(), "Run evidence remains available after cleanup"
    config = yaml.safe_load((runs[0] / "hermes/config.yaml").read_text(encoding="utf-8"))
    assert config["agent"] == {"api_max_retries": 1, "auto_recovery_cycles": 0}
    assert config["fallback_providers"] == []
    assert config["providers"]["gemini"]["enabled"] is False
    assert config["antigravity_cli"]["experimental_subscription_bridge"] is True
    assert not list(runs[0].rglob("oauth_creds.json"))
    assert not list(runs[0].rglob("auth.json"))

    if fail_response == "yes":
        assert result["completed"] is False and result["error_present"] is True
        assert result["marker_received"] is False
        assert result["error_category"] == "authentication"
        assert "authentication failure" not in result["response"]
    else:
        assert result["completed"] is True and result["error_present"] is False
        assert result["marker_received"] is True
        assert result["response"] == probe_module.MARKER
    assert result["client"] == "AntigravityCLIClient"
    assert result["provider"] == "antigravity-cli"
    Path(output).write_text(json.dumps(result), encoding="utf-8")
finally:
    caller_logger.removeHandler(caller_handler)
    caller_handler.close()
'''


@pytest.mark.parametrize("fail_response", [False, True], ids=["marker", "failure-no-retry"])
def test_real_hermes_probe_is_offline_isolated_and_releases_logs(tmp_path, fail_response):
    """Exercise real registration, initialization, conversation, and cleanup."""
    hermes_home = tmp_path / "caller-hermes"
    native_home = tmp_path / "caller-native"
    for directory in (hermes_home, native_home):
        directory.mkdir()
    environment = os.environ.copy()
    # Deliberately populate credentials and proxy settings with fake sentinels so
    # temporary removal/overrides and restoration are observed inside the child.
    environment.update({
        "HERMES_HOME": str(hermes_home),
        "HERMES_RUNTIME_DIR": str(tmp_path / "caller-runtime"),
        "USERPROFILE": str(native_home), "HOME": str(native_home),
        "GEMINI_API_KEY": "offline-fake-gemini-key",
        "GOOGLE_API_KEY": "offline-fake-google-key",
        "GOOGLE_GEMINI_BASE_URL": "https://offline-test.invalid",
        "HTTP_PROXY": "http://127.0.0.1:43211",
        "HTTPS_PROXY": "http://127.0.0.1:43211",
        "ALL_PROXY": "socks5://127.0.0.1:43212",
        "NO_PROXY": "caller-test.invalid",
    })
    script = tmp_path / "check_probe.py"
    script.write_text(CHILD_CHECK, encoding="utf-8")
    result_file = tmp_path / "result.json"
    completed = subprocess.run(
        [sys.executable, str(script), str(PROJECT_ROOT), str(tmp_path / "project"),
         str(result_file), "yes" if fail_response else "no"],
        cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["marker_received"] is not fail_response
