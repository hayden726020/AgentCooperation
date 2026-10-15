"""Offline behavior checks: intercept the executable boundary without model calls."""

import json
import base64
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gemini_dev.ps1"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PWSH or os.name != "nt", reason="Windows PowerShell required")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def run_launcher(tmp_path):
    home = tmp_path / "isolated home"
    settings = home / ".gemini/antigravity-cli/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"modelProvider":"antigravity"}', encoding="utf-8")
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    executable = tmp_path / "native bin/agy.exe"
    executable.parent.mkdir()
    executable.touch()
    capture = tmp_path / "capture.json"
    after = tmp_path / "after.json"

    def run(*arguments, provider="antigravity", exit_code=0, raw_settings=None, script=SCRIPT, native_options=True, restore_failure=False, invocation_failure=False):
        settings.write_text(raw_settings if raw_settings is not None else json.dumps({"modelProvider": provider}), encoding="utf-8")
        capture.unlink(missing_ok=True)
        after.unlink(missing_ok=True)
        options = {"-AgyExecutable": executable, "-Workspace": workspace} if native_options else {}
        iterator = iter(arguments)
        for argument in iterator:
            options[argument] = None if argument == "-DryRun" else next(iterator)
        invocation_args = " ".join(name if value is None else name + " " + ps_literal(value) for name, value in options.items())
        harness = tmp_path / "harness.ps1"
        # A full-path function replaces only the native invocation in this isolated process.
        harness.write_text(
            f"""$ErrorActionPreference = 'Stop'
$env:USERPROFILE = {ps_literal(home)}
$env:HOME = {ps_literal(home)}
$env:GEMINI_API_KEY = 'isolated-gemini-secret'
$env:GOOGLE_API_KEY = 'isolated-google-secret'
$env:GOOGLE_GEMINI_BASE_URL = 'https://isolated.invalid'
$originalCwd = (Get-Location).Path
$global:restoreFailPath = $originalCwd
if (${str(restore_failure).lower()}) {{
    function global:Set-Location {{
        param([string] $LiteralPath)
        if ($LiteralPath -eq $global:restoreFailPath) {{ throw 'Injected restore failure' }}
        Microsoft.PowerShell.Management\\Set-Location -LiteralPath $LiteralPath
    }}
}}
Set-Item -LiteralPath {ps_literal('Function:' + str(executable))} -Value {{
    $capturedArgs = @($args)
    @{{ args = $capturedArgs; cwd = (Get-Location).Path;
        gemini = $env:GEMINI_API_KEY; google = $env:GOOGLE_API_KEY;
        endpoint = $env:GOOGLE_GEMINI_BASE_URL; home = $env:USERPROFILE }} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath {ps_literal(capture)} -Encoding utf8
    if (${str(invocation_failure).lower()}) {{ throw 'Injected invocation failure' }}
    $global:LASTEXITCODE = {exit_code}
}}
$failed = $false
try {{
    & {ps_literal(script)} {invocation_args}
    $resultCode = $LASTEXITCODE
}} catch {{
    $failed = $true
    $resultCode = 1
    [Console]::Error.WriteLine($_.Exception.Message)
}} finally {{
    @{{ cwd = (Get-Location).Path; originalCwd = $originalCwd;
        gemini = $env:GEMINI_API_KEY; google = $env:GOOGLE_API_KEY;
        endpoint = $env:GOOGLE_GEMINI_BASE_URL; home = $env:USERPROFILE }} |
        ConvertTo-Json | Set-Content -LiteralPath {ps_literal(after)} -Encoding utf8
}}
exit $resultCode
""", encoding="utf-8")
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", cwd=tmp_path)
        return result, json.loads(capture.read_text(encoding="utf-8-sig")) if capture.exists() else None, json.loads(after.read_text(encoding="utf-8-sig"))

    run.workspace = workspace
    run.home = home
    run.tmp = tmp_path
    run.executable = executable
    return run


def test_native_interactive_without_automatic_task(run_launcher):
    result, capture, after = run_launcher()
    assert result.returncode == 0, result.stderr
    assert capture["args"] == ["--model", "gemini-3.8-flash-high", "--effort", "high"]
    assert capture["cwd"] == str(run_launcher.workspace)
    assert capture["home"] == str(run_launcher.home)
    assert not capture["gemini"] and not capture["google"] and not capture["endpoint"]
    assert after["cwd"] == after["originalCwd"]
    assert after["gemini"] == "isolated-gemini-secret"
    assert after["google"] == "isolated-google-secret"
    assert after["endpoint"] == "https://isolated.invalid"


def test_task_text_is_passed_literally_and_environment_restored_on_failure(run_launcher):
    task = '中文任务\n"quoted" $([IO.File]::WriteAllText("should-not-exist", "bad"))\nsecond line'
    task_file = run_launcher.tmp / "task with spaces.txt"
    task_file.write_text(task, encoding="utf-8", newline="")
    result, capture, after = run_launcher("-TaskFile", task_file.name, exit_code=7)
    assert result.returncode == 7, result.stderr
    assert capture["args"][-2:] == ["--prompt-interactive", task]
    assert not (run_launcher.tmp / "should-not-exist").exists()
    assert after["cwd"] == after["originalCwd"]
    assert after["gemini"] == "isolated-gemini-secret"


def test_dry_run_does_not_execute_or_disclose_task_or_credentials(run_launcher):
    task_file = run_launcher.tmp / "task.txt"
    task_file.write_text("private task content", encoding="utf-8")
    result, capture, after = run_launcher("-TaskFile", str(task_file), "-DryRun")
    assert result.returncode == 0, result.stderr
    assert capture is None
    diagnostics = json.loads(result.stdout)
    assert diagnostics["model"] == "gemini-3.8-flash-high"
    assert diagnostics["effort"] == "high"
    assert diagnostics["task_present"] is True
    assert "private task" not in result.stdout
    assert "isolated-gemini-secret" not in result.stdout
    assert after["gemini"] == "isolated-gemini-secret"


@pytest.mark.parametrize("provider", ["gemini", "unrecognized-provider"])
def test_api_or_unknown_provider_is_rejected(run_launcher, provider):
    result, capture, _ = run_launcher(provider=provider)
    assert result.returncode != 0
    assert capture is None


def test_invalid_settings_are_rejected_without_contents(run_launcher):
    result, capture, _ = run_launcher(raw_settings="private broken credential fragment")
    assert result.returncode != 0
    assert capture is None
    assert "private broken" not in result.stderr


@pytest.mark.parametrize("contents", [None, "", " \n\t", "hello\x00world"])
def test_missing_empty_or_nul_task_is_rejected(run_launcher, contents):
    task = run_launcher.tmp / "task.txt"
    if contents is not None:
        task.write_text(contents, encoding="utf-8")
    result, capture, _ = run_launcher("-TaskFile", str(task))
    assert result.returncode != 0
    assert capture is None


def test_non_executable_override_is_rejected(run_launcher):
    command = run_launcher.tmp / "unsafe.cmd"
    command.write_text("echo should not run", encoding="utf-8")
    result, capture, _ = run_launcher("-AgyExecutable", str(command))
    assert result.returncode != 0
    assert capture is None


def test_missing_workspace_is_rejected(run_launcher):
    result, capture, _ = run_launcher("-Workspace", str(run_launcher.tmp / "missing directory"))
    assert result.returncode != 0
    assert capture is None


def test_other_executable_override_is_rejected(run_launcher):
    command = run_launcher.tmp / "python.exe"
    command.touch()
    result, capture, _ = run_launcher("-AgyExecutable", str(command))
    assert result.returncode != 0
    assert "native agy.exe" in result.stderr
    assert capture is None


def test_default_workspace_is_project_root(run_launcher):
    result, capture, _ = run_launcher("-AgyExecutable", str(run_launcher.executable), "-DryRun", native_options=False)
    assert result.returncode == 0, result.stderr
    assert capture is None
    assert json.loads(result.stdout)["workspace"] == str(SCRIPT.parent.parent)


def test_environment_is_restored_even_when_original_directory_restore_fails(run_launcher):
    result, capture, after = run_launcher(restore_failure=True)
    assert result.returncode != 0
    assert "Injected restore failure" in result.stderr
    assert capture is not None
    assert after["gemini"] == "isolated-gemini-secret"
    assert after["google"] == "isolated-google-secret"
    assert after["endpoint"] == "https://isolated.invalid"


def test_directory_and_environment_restore_after_invocation_exception(run_launcher):
    result, capture, after = run_launcher(invocation_failure=True)
    assert result.returncode != 0
    assert "Injected invocation failure" in result.stderr
    assert capture is not None
    assert after["cwd"] == after["originalCwd"]
    assert after["gemini"] == "isolated-gemini-secret"
    assert after["google"] == "isolated-google-secret"
    assert after["endpoint"] == "https://isolated.invalid"


def test_direct_entry_works_without_hermes_profiles(run_launcher):
    hermes_script = SCRIPT.with_name("hermes_bots.ps1")
    result, capture, _ = run_launcher(
        "-Action", "direct", "-Bot", "coop-dev", "-HermesHome", str(run_launcher.tmp / "absent hermes"),
        "-DryRun", script=hermes_script,
    )
    assert result.returncode == 0, result.stderr
    assert capture is None
    assert json.loads(result.stdout)["mode"] == "native-interactive"


def test_direct_rejects_other_bot(run_launcher):
    result, capture, _ = run_launcher("-Action", "direct", "-Bot", "coop-review", "-DryRun", script=SCRIPT.with_name("hermes_bots.ps1"))
    assert result.returncode != 0
    assert "only for coop-dev" in result.stderr
    assert capture is None


@pytest.mark.parametrize("option", ["-Workspace", "-TaskFile", "-AgyExecutable"])
def test_direct_rejects_explicit_empty_option(run_launcher, option):
    result, capture, _ = run_launcher(
        "-Action", "direct", "-Bot", "coop-dev", option, "", "-DryRun", script=SCRIPT.with_name("hermes_bots.ps1"),
    )
    assert result.returncode != 0
    assert capture is None


def test_hermes_provider_chat_remains_blocked(run_launcher):
    hermes_home = run_launcher.tmp / "hermes"
    profile = hermes_home / "profiles/coop-dev"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  provider: antigravity-cli\n", encoding="utf-8")
    result, capture, _ = run_launcher(
        "-Action", "chat", "-Bot", "coop-dev", "-HermesHome", str(hermes_home),
        script=SCRIPT.with_name("hermes_bots.ps1"), native_options=False,
    )
    assert result.returncode != 0
    assert "model-provider chat remains blocked" in result.stderr
    assert capture is None


@pytest.mark.parametrize("option,value", [("-Workspace", "somewhere"), ("-TaskFile", "task.txt"), ("-DryRun", None)])
def test_non_direct_actions_reject_native_options(run_launcher, option, value):
    arguments = ["-Action", "chat", "-Bot", "coop-dev", option]
    if value is not None:
        arguments.append(value)
    result, capture, _ = run_launcher(*arguments, script=SCRIPT.with_name("hermes_bots.ps1"), native_options=False)
    assert result.returncode != 0
    assert "only supported by the direct action" in result.stderr
    assert capture is None


@pytest.mark.parametrize("script_filename,extra_args", [("gemini_dev.ps1", []), ("hermes_bots.ps1", ["-Action", "direct", "-Bot", "coop-dev"])])
def test_real_native_argument_boundary_preserves_utf8_multiline_and_quotes(tmp_path, script_filename, extra_args):
    """Compile a local fake agy.exe; no installed CLI, login or model is contacted."""
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("Local .NET Framework compiler unavailable")
    bin_dir = tmp_path / "fake native bin"
    bin_dir.mkdir()
    executable = bin_dir / "agy.exe"
    source = bin_dir / "fake.cs"
    source.write_text('''using System;
using System.IO;
using System.Text;
class FakeAgy {
    static int Main(string[] args) {
        string[] encoded = new string[args.Length];
        for (int i = 0; i < args.Length; i++) encoded[i] = Convert.ToBase64String(Encoding.UTF8.GetBytes(args[i]));
        File.WriteAllLines(Environment.GetEnvironmentVariable("AGY_CAPTURE_PATH"), encoded);
        return 7;
    }
}''', encoding="utf-8")
    compile_result = subprocess.run([str(compiler), "/nologo", "/out:" + str(executable), str(source)], capture_output=True, text=True)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    home = tmp_path / "isolated home"
    home.mkdir()
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    task = '中文任务\n"quoted" $([IO.File]::WriteAllText("unexpected.txt", "bad"))\nfinal line'
    task_file = tmp_path / "task with spaces.txt"
    task_file.write_text(task, encoding="utf-8", newline="")
    capture = tmp_path / "captured-argv.txt"
    environment = dict(os.environ, USERPROFILE=str(home), HOME=str(home), AGY_CAPTURE_PATH=str(capture))
    result = subprocess.run([
        PWSH, "-NoProfile", "-File", str(SCRIPT.with_name(script_filename)), *extra_args, "-Workspace", str(workspace),
        "-AgyExecutable", str(executable), "-TaskFile", str(task_file),
    ], env=environment, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 7, result.stderr
    captured = [base64.b64decode(line).decode("utf-8") for line in capture.read_text().splitlines()]
    assert captured == ["--model", "gemini-3.8-flash-high", "--effort", "high", "--prompt-interactive", task]
    assert not (workspace / "unexpected.txt").exists()
