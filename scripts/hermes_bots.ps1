#requires -Version 5.1
param(
    [ValidateSet('list', 'show', 'chat', 'direct')]
    [string] $Action = 'list',
    [ValidateSet('coop-dev', 'coop-review', 'coop-expert')]
    [string] $Bot = 'coop-review',
    [string] $HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'hermes' } else { Join-Path $env:USERPROFILE 'AppData\Local\hermes' }),
    [string] $Python = 'python',
    [string] $Workspace = '',
    [string] $TaskFile = '',
    [string] $AgyExecutable = '',
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandArgumentPassing = 'Standard'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path $PSScriptRoot -Parent
$hermesSource = Join-Path $projectRoot 'hermes-agent'
if ($Action -eq 'direct') {
    if ($Bot -ne 'coop-dev') {
        throw "The direct action is only for coop-dev. Use chat for other Hermes profiles."
    }
    # A user-owned foreground CLI session; no Hermes model provider or tool loop.
    $directOptions = @{ DryRun = $DryRun }
    if ($PSBoundParameters.ContainsKey('Workspace')) { $directOptions.Workspace = $Workspace }
    if ($PSBoundParameters.ContainsKey('TaskFile')) { $directOptions.TaskFile = $TaskFile }
    if ($PSBoundParameters.ContainsKey('AgyExecutable')) { $directOptions.AgyExecutable = $AgyExecutable }
    & (Join-Path $PSScriptRoot 'gemini_dev.ps1') @directOptions
    exit $LASTEXITCODE
}
if ($PSBoundParameters.ContainsKey('DryRun') -or $PSBoundParameters.ContainsKey('Workspace') -or
    $PSBoundParameters.ContainsKey('TaskFile') -or $PSBoundParameters.ContainsKey('AgyExecutable')) {
    throw "Workspace, TaskFile, AgyExecutable and DryRun are only supported by the direct action."
}
if ($Action -eq 'list') {
    & $Python (Join-Path $PSScriptRoot 'configure_hermes_bots.py') --home $HermesHome --check
    exit $LASTEXITCODE
}

$profileRoot = Join-Path (Join-Path $HermesHome 'profiles') $Bot
$configPath = Join-Path $profileRoot 'config.yaml'
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Profile $Bot is not configured. Run configure_hermes_bots.py first."
}
if ($Action -eq 'show') {
    # Config and role identity contain no copied credentials; never display .env/auth.json.
    Get-Content -LiteralPath (Join-Path $profileRoot 'profile.yaml') -Encoding utf8
    Get-Content -LiteralPath (Join-Path $profileRoot 'SOUL.md') -Encoding utf8
    exit 0
}

if ($Bot -eq 'coop-dev') {
    $rawConfig = Get-Content -LiteralPath $configPath -Raw -Encoding utf8
    if ($rawConfig -notmatch 'antigravity-cli') {
        throw "coop-dev Antigravity CLI channel is not configured. Run configure_hermes_bots.py first."
    }
    # Resolve YAML semantically, so comments or the string "true" cannot enable it.
    $bridgeEnabled = & $Python -c 'import sys,yaml; c=yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {}; print("enabled" if c.get("antigravity_cli",{}).get("experimental_subscription_bridge") is True else "disabled")' $configPath
    if ($LASTEXITCODE -ne 0) { throw "Could not read Gemini bridge configuration." }
    if ($bridgeEnabled -ne 'enabled') {
        throw "Hermes model-provider chat remains blocked. Enable the experimental bridge in the managed profile, or use ./scripts/hermes_bots.ps1 direct coop-dev."
    }
    Write-Host "Starting experimental Hermes Gemini High conversation bridge via the native subscription CLI."
    $projectInfo = & $Python -c 'import json,sys,yaml; c=yaml.safe_load(open(sys.argv[1],encoding="utf-8")) or {}; p=c.get("antigravity_cli",{}).get("project_execution",{}); print(json.dumps({"enabled": p.get("enabled") is True, "workspace": p.get("workspace")}))' $configPath
    if ($LASTEXITCODE -ne 0) { throw "Could not read Gemini project execution configuration." }
    $projectState = $projectInfo | ConvertFrom-Json
    if ($projectState.enabled) {
        Write-Host "Bounded project execution is enabled for: $($projectState.workspace)"
        Write-Host "Native tools may read and edit that workspace and run the limited local test allowlist. Gemini API fallback remains disabled."
    } else {
        Write-Host "Project execution is disabled; this session is text-only. Gemini API fallback remains disabled."
    }
}
if ($Bot -eq 'coop-expert') {
    Write-Host "Starting manual Codex Expert chat (gpt-6.1-sol via local Codex CLI app-server)."
    Write-Host "Authentication is managed by Codex CLI itself (~/.codex/auth.json); no OAuth files copied into Hermes."
    Write-Host "Read-only enforcement active: file changes, command executions, and MCP tool calls are declined at CLI extra_args and protocol layers."
}
if ($Bot -eq 'coop-review') {
    Write-Host "Starting manual GLM FlashX chat. Model requests may incur provider charges. This profile has no file, terminal, MCP, or orchestration tools."
}

$oldHermesHome = $env:HERMES_HOME
$oldPythonPath = $env:PYTHONPATH
try {
    $env:HERMES_HOME = $HermesHome
    $env:PYTHONPATH = if ($oldPythonPath) { "$hermesSource$([IO.Path]::PathSeparator)$oldPythonPath" } else { $hermesSource }
    & $Python -m hermes_cli.main -p $Bot chat
    $chatExit = $LASTEXITCODE
} finally {
    $env:HERMES_HOME = $oldHermesHome
    $env:PYTHONPATH = $oldPythonPath
}
exit $chatExit
