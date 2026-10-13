"""Real local transport checks; helpers never contact a model service."""

import importlib
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.codex_expert import CodexExpert
from agents.gemini_reviewer import GeminiReviewer
from agents.opencode_runner import OpenCodeRunner
from config.settings import settings
from execution.subproc import SubprocResult, run_async_command


def review_output():
    return json.dumps({
        "approved": True, "severity": "P3", "summary": "fixture",
        "critical_issues": [], "suggestions": [],
    })


def launcher():
    return importlib.import_module("execution.cli_launcher")


def package_fixture(root: Path, bin_value=None):
    package = root / ".runtime/gemini/node_modules/@google/gemini-cli"
    package.mkdir(parents=True)
    entry = package / "dist/index.js"
    entry.parent.mkdir()
    entry.write_text("// local fixture", encoding="utf-8")
    manifest = {
        "name": "@google/gemini-cli", "version": "0.63.0",
        "bin": {"gemini": "dist/index.js"} if bin_value is None else bin_value,
    }
    (package / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package, entry


def test_default_gemini_discovers_local_manifest_without_shell(monkeypatch, tmp_path):
    module = launcher()
    _, entry = package_fixture(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda cmd: sys.executable if cmd == "node" else None)
    assert module.build_cli_argv("gemini", repository_root=tmp_path) == [sys.executable, str(entry.resolve())]


def test_gemini_string_bin_is_supported(monkeypatch, tmp_path):
    module = launcher()
    _, entry = package_fixture(tmp_path, "dist/index.js")
    monkeypatch.setattr(module.shutil, "which", lambda cmd: sys.executable if cmd == "node" else None)
    assert module.build_cli_argv("gemini", repository_root=tmp_path)[1] == str(entry.resolve())


def test_explicit_prefix_takes_priority_over_default_local_package(tmp_path):
    package_fixture(tmp_path)
    prefix = [sys.executable, str(tmp_path / "wrapper with spaces.py")]
    assert launcher().build_cli_argv("gemini", prefix, repository_root=tmp_path) == prefix


def test_explicit_command_takes_priority_over_local_gemini(monkeypatch, tmp_path):
    module = launcher()
    package_fixture(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda cmd: None)
    assert module.build_cli_argv("custom-gemini", repository_root=tmp_path) == ["custom-gemini"]


@pytest.mark.parametrize("prefix", ["node script.js", [""], ["node", ""], ["node", 3], ["node", "bad\0arg"]])
def test_prefix_rejects_invalid_argv(prefix, tmp_path):
    with pytest.raises(ValueError):
        launcher().build_cli_argv("fixture", prefix, repository_root=tmp_path)


@pytest.mark.parametrize("command", ["gemini.cmd", "fixture.BAT", "C:/tools/wrapper.ps1", "cmd.exe", "powershell.exe", "bash"])
def test_batch_and_shell_executables_are_rejected(command, monkeypatch, tmp_path):
    module = launcher()
    monkeypatch.setattr(module.shutil, "which", lambda cmd: None)
    with pytest.raises(ValueError, match="shell|argv prefix"):
        module.build_cli_argv(command, repository_root=tmp_path)


def test_path_resolved_npm_shim_requires_explicit_prefix(monkeypatch, tmp_path):
    module = launcher()
    monkeypatch.setattr(module.shutil, "which", lambda cmd: "C:/npm/opencode.cmd")
    with pytest.raises(ValueError, match="argv prefix"):
        module.build_cli_argv("opencode", repository_root=tmp_path)


@pytest.mark.parametrize("bin_value", ["../../../../../escape.js", {"other": "dist/index.js"}, ["dist/index.js"], "missing.js"])
def test_local_package_rejects_unsafe_or_missing_entry(bin_value, monkeypatch, tmp_path):
    module = launcher()
    package_fixture(tmp_path, bin_value)
    monkeypatch.setattr(module.shutil, "which", lambda cmd: sys.executable)
    with pytest.raises(ValueError):
        module.build_cli_argv("gemini", repository_root=tmp_path)


def test_existing_local_package_without_node_fails_closed(monkeypatch, tmp_path):
    module = launcher()
    package_fixture(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda cmd: None)
    with pytest.raises(ValueError, match="Node"):
        module.build_cli_argv("gemini", repository_root=tmp_path)


@pytest.mark.asyncio
async def test_real_echo_handles_large_unicode_and_metacharacters_without_shell(tmp_path):
    helper = tmp_path / "space folder" / "echo helper.py"
    helper.parent.mkdir()
    helper.write_text(
        "import json,sys\n"
        "sys.stdin.reconfigure(encoding='utf-8')\n"
        "print(json.dumps({'argv':sys.argv[1:],'input':sys.stdin.read()}))\n",
        encoding="utf-8",
    )
    payload = ('中文🚀 \"quoted\" & | < > %PATH% !HOME! $() `backtick`\r\n' * 3000)
    assert len(payload.encode("utf-8")) > 40_000
    prefix = launcher().build_cli_argv("fixture", [sys.executable, str(helper)])
    outcome = await run_async_command(prefix + ["arg with spaces & | %"], cwd=tmp_path, input_text=payload)
    assert outcome.is_success, outcome.stderr
    decoded = json.loads(outcome.stdout)
    assert decoded["argv"] == ["arg with spaces & | %"]
    # Python universal-newline stdin normalization does not alter any data tokens.
    assert decoded["input"] == payload.replace("\r\n", "\n")


@pytest.mark.asyncio
async def test_real_node_echo_preserves_large_input_bytes(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node runtime unavailable")
    helper = tmp_path / "space folder" / "echo helper.js"
    helper.parent.mkdir()
    helper.write_text(
        "let data='';process.stdin.setEncoding('utf8');"
        "process.stdin.on('data',chunk=>data+=chunk);"
        "process.stdin.on('end',()=>console.log(JSON.stringify({args:process.argv.slice(2),data})));",
        encoding="utf-8",
    )
    payload = ('中文🚀 & | > < %PATH% !value! \"quoted\"\r\n' * 3000)
    prefix = launcher().build_cli_argv("fixture", [node, str(helper)])
    outcome = await run_async_command(prefix + ["literal & | %"], cwd=tmp_path, input_text=payload)
    assert outcome.is_success, outcome.stderr
    assert json.loads(outcome.stdout) == {"args": ["literal & | %"], "data": payload}


@pytest.mark.asyncio
async def test_gemini_uses_explicit_prefix_and_stdin_context(monkeypatch, tmp_path):
    prefix = [sys.executable, str(tmp_path / "gemini wrapper.py")]
    monkeypatch.setitem(settings.__dict__, "gemini_cli_argv_prefix", prefix)
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, review_output(), "", 0))
    monkeypatch.setattr("agents.gemini_reviewer.run_async_command", command)
    requirement = "需求 & | %PATH%\n" * 5000
    answer = await GeminiReviewer().execute("Review this task", {"worktree_path": tmp_path, "requirement": requirement})
    assert answer.success
    argv = command.call_args.args[0]
    assert argv[:2] == prefix
    assert requirement not in " ".join(argv)
    assert requirement in command.call_args.kwargs["input_text"]
    assert argv[argv.index("--approval-mode") + 1] == "plan"


@pytest.mark.asyncio
async def test_codex_preserves_subscription_home_and_passes_prompt_on_stdin(monkeypatch, tmp_path):
    prefix = [sys.executable, str(tmp_path / "codex wrapper.py")]
    monkeypatch.setitem(settings.__dict__, "codex_cli_argv_prefix", prefix)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "account home"))
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fixture")
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, review_output(), "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    prompt = "问题 中文 &|%\n" * 5000
    answer = await CodexExpert().execute(prompt, {"worktree_path": tmp_path, "structured_review": True})
    assert answer.success and answer.metadata["review"]["approved"]
    argv = command.call_args.args[0]
    assert argv[:2] == prefix and argv[-1] == "-"
    assert prompt not in " ".join(argv)
    assert prompt in command.call_args.kwargs["input_text"]
    assert command.call_args.kwargs["env"] == {"CODEX_HOME": str(tmp_path / "account home")}
    assert "--ephemeral" in argv
    assert argv[argv.index("--color") + 1] == "never"
    assert "--ignore-user-config" not in argv


@pytest.mark.asyncio
async def test_codex_does_not_invent_auth_home_or_pass_provider_keys(monkeypatch, tmp_path):
    monkeypatch.setitem(settings.__dict__, "codex_cli_argv_prefix", [])
    monkeypatch.delenv("CODEX_HOME", raising=False)
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, "advice", "", 0))
    monkeypatch.setattr("agents.codex_expert.run_async_command", command)
    assert (await CodexExpert().execute("fixture", {"worktree_path": tmp_path})).success
    assert command.call_args.kwargs["env"] == {}


@pytest.mark.asyncio
async def test_opencode_supports_explicit_literal_prefix_without_inventing_stdin(monkeypatch, tmp_path):
    prefix = [sys.executable, str(tmp_path / "opencode wrapper.py")]
    monkeypatch.setitem(settings.__dict__, "opencode_cli_argv_prefix", prefix)
    command = AsyncMock(return_value=SubprocResult(["fixture"], 0, "fixture", "", 0))
    monkeypatch.setattr("agents.opencode_runner.run_async_command", command)
    answer = await OpenCodeRunner("unknown/fixture").execute("literal &|% prompt", {"worktree_path": tmp_path})
    assert answer.success
    assert command.call_args.kwargs.get("input_text") is None
    assert command.call_args.kwargs["cmd"][:2] == prefix
    assert command.call_args.kwargs["cmd"][-1] == "literal &|% prompt"
