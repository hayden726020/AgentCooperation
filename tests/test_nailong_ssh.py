"""Offline behavior tests for the Windows-side Nailong SSH launcher.

The tests execute PowerShell in an isolated process and never contact a real
SSH server.  Native SSH behavior is represented only by the launcher's dry-run
description.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nailong_ssh.ps1"
INSTALLER = ROOT / "scripts" / "install_nailong_ssh.ps1"
POWERSHELLS = [
    executable
    for executable in (shutil.which("pwsh"), shutil.which("powershell"))
    if executable
]

pytestmark = pytest.mark.skipif(not POWERSHELLS, reason="PowerShell is required")


def ps_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(executable: str, body: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
        + body
    )
    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        cwd=cwd or ROOT,
    )


@pytest.fixture(params=POWERSHELLS, ids=lambda value: Path(value).stem)
def powershell(request) -> str:
    return request.param


def test_dry_run_uses_safe_defaults_and_two_distinct_ssh_phases(powershell):
    result = run_ps(
        powershell,
        f"& {ps_literal(SCRIPT)} 'server.example' -DryRun | ConvertTo-Json -Depth 6",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["destination"] == "spcuteag@server.example"
    assert payload["port"] == 22
    assert payload["probe_arguments"][-2:] == ["spcuteag@server.example", "sh -s"]
    assert payload["interactive_arguments"][-1] == "spcuteag@server.example"
    assert "StrictHostKeyChecking=no" not in result.stdout


@pytest.mark.parametrize(
    "server,user,port,expected_error",
    [
        ("-oProxyCommand=bad", "spcuteag", 22, "Invalid server"),
        ("host name", "spcuteag", 22, "Invalid server"),
        ("server.example", "bad;user", 22, "Invalid user"),
        ("server.example", "spcuteag", 0, "Port must"),
        ("server.example", "spcuteag", 65536, "Port must"),
    ],
)
def test_rejects_injection_shaped_connection_parameters(
    powershell, server, user, port, expected_error
):
    result = run_ps(
        powershell,
        f"& {ps_literal(SCRIPT)} {ps_literal(server)} -User {ps_literal(user)} "
        f"-Port {port} -DryRun",
    )
    assert result.returncode != 0
    assert expected_error in result.stderr


def test_probe_parser_ignores_noise_and_strips_terminal_controls(powershell):
    body = f"""
. {ps_literal(SCRIPT)}
$esc = [char]27
$lines = @(
    'Ubuntu banner noise',
    '__NAILONG_BEGIN__',
    ('HOSTNAME=' + $esc + '[31mmilk-host' + $esc + '[0m'),
    'OS=Ubuntu 24.04.1 LTS',
    'UPTIME=2 days, 3 hours',
    'LOAD=0.10 0.20 0.30',
    'MEMORY=512/2048 MiB (25%)',
    'DISK=1024/4096 MiB (25%)',
    'IP=192.168.10.8',
    '__NAILONG_END__',
    'ignored=value'
)
ConvertFrom-NailongProbeOutput -Lines $lines | ConvertTo-Json
"""
    result = run_ps(powershell, body)
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts == {
        "Hostname": "milk-host",
        "OS": "Ubuntu 24.04.1 LTS",
        "Uptime": "2 days, 3 hours",
        "Load": "0.10 0.20 0.30",
        "Memory": "512/2048 MiB (25%)",
        "Disk": "1024/4096 MiB (25%)",
        "IP": "192.168.10.8",
    }


def test_layout_and_static_view_adapt_to_terminal_width(powershell):
    body = f"""
. {ps_literal(SCRIPT)}
$facts = [pscustomobject]@{{
    Hostname='milk-host'; OS='Ubuntu 24.04 LTS'; Uptime='3 hours';
    Load='0.10 0.20 0.30'; Memory='512/2048 MiB (25%)';
    Disk='1024/4096 MiB (25%)'; IP='192.168.1.8'
}}
@{{
    wide = Get-NailongLayout -Width 100
    compact = Get-NailongLayout -Width 70
    mini = Get-NailongLayout -Width 40
    wideView = Get-NailongStaticView -Facts $facts -Width 100 -UseColor:$false
    miniView = Get-NailongStaticView -Facts $facts -Width 40 -UseColor:$false
}} | ConvertTo-Json
"""
    result = run_ps(powershell, body)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["wide"], payload["compact"], payload["mini"]) == (
        "Wide",
        "Compact",
        "Mini",
    )
    assert "SPCUTEAG的奶服" in payload["wideView"]
    assert "奶龙已上线，开始工作" in payload["wideView"]
    assert "192.168.1.8" in payload["miniView"]
    assert "\x1b[" not in payload["wideView"] + payload["miniView"]


def test_color_detection_honors_explicit_and_environment_fallbacks(powershell):
    body = f"""
. {ps_literal(SCRIPT)}
$oldNoColor = $env:NO_COLOR
$oldTerm = $env:TERM
try {{
    $env:NO_COLOR = $null
    $env:TERM = 'xterm-256color'
    $normal = Test-NailongColorSupport -NoColor:$false -IsOutputRedirected:$false -SupportsVirtualTerminal:$true
    $explicit = Test-NailongColorSupport -NoColor:$true -IsOutputRedirected:$false -SupportsVirtualTerminal:$true
    $env:NO_COLOR = '1'
    $environment = Test-NailongColorSupport -NoColor:$false -IsOutputRedirected:$false -SupportsVirtualTerminal:$true
    $env:NO_COLOR = $null
    $env:TERM = 'dumb'
    $dumb = Test-NailongColorSupport -NoColor:$false -IsOutputRedirected:$false -SupportsVirtualTerminal:$true
    @{{ normal=$normal; explicit=$explicit; environment=$environment; dumb=$dumb }} | ConvertTo-Json
}} finally {{
    $env:NO_COLOR = $oldNoColor
    $env:TERM = $oldTerm
}}
"""
    result = run_ps(powershell, body)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "normal": True,
        "explicit": False,
        "environment": False,
        "dumb": False,
    }


def test_animation_plan_has_requested_story_and_duration(powershell):
    body = f"""
. {ps_literal(SCRIPT)}
$plan = @(Get-NailongAnimationPlan -Layout Wide)
@{{
    stages = @($plan | ForEach-Object {{ $_.Stage }})
    total = ($plan | Measure-Object -Property DurationMs -Sum).Sum
}} | ConvertTo-Json
"""
    result = run_ps(powershell, body)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["stages"][0] == "Appear"
    assert "BellyLaugh" in payload["stages"]
    assert payload["stages"][-1] == "Final"
    assert 1500 <= payload["total"] <= 2000


def test_remote_probe_is_fixed_read_only_and_does_not_use_public_network(powershell):
    body = f"""
. {ps_literal(SCRIPT)}
Get-NailongRemoteProbeScript
"""
    result = run_ps(powershell, body)
    assert result.returncode == 0, result.stderr
    probe = result.stdout
    assert "__NAILONG_BEGIN__" in probe and "__NAILONG_END__" in probe
    assert "/etc/os-release" in probe
    assert "hostname -I" in probe
    assert all(token not in probe.lower() for token in ("curl ", "wget ", "http://", "https://"))
    assert all(token not in probe.lower() for token in ("sudo ", "apt ", "rm ", "tee "))


def test_installer_is_idempotent_and_uninstall_preserves_existing_profile(powershell, tmp_path):
    profile = tmp_path / "profile.ps1"
    install_root = tmp_path / "installed Nailong"
    profile.write_text("# keep me\n$global:ExistingSetting = 1\n", encoding="utf-8")
    install = (
        f"& {ps_literal(INSTALLER)} -ProfilePath {ps_literal(profile)} "
        f"-InstallRoot {ps_literal(install_root)}"
    )
    first = run_ps(powershell, install)
    second = run_ps(powershell, install)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    installed_script = install_root / "nailong_ssh.ps1"
    assert installed_script.exists()
    contents = profile.read_text(encoding="utf-8-sig")
    assert contents.count("# >>> nailong-ssh >>>") == 1
    assert "# keep me" in contents
    assert str(installed_script).replace("'", "''") in contents

    uninstall = run_ps(
        powershell,
        install + " -Uninstall",
    )
    assert uninstall.returncode == 0, uninstall.stderr
    assert not install_root.exists()
    remaining = profile.read_text(encoding="utf-8-sig")
    assert "nailong-ssh" not in remaining
    assert "# keep me" in remaining

