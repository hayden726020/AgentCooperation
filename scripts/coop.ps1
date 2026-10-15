#requires -Version 5.1
<#
.SYNOPSIS
    Convenient wrapper for AgentCooperation CLI (`hermes coop`).

.EXAMPLE
    .\scripts\coop.ps1 budget
    .\scripts\coop.ps1 list
    .\scripts\coop.ps1 loop "Add feature" --mock --repo "E:\AgentCooperation" --test-path "tests/test_fsm.py"
    .\scripts\coop.ps1 status <task_id>
#>

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$cliScript = Join-Path $projectRoot 'hermes-agent\plugins\agent_cooperation\cli.py'

if (-not (Test-Path -LiteralPath $cliScript)) {
    Write-Error "Could not find coop CLI at: $cliScript"
    exit 1
}

& python $cliScript @CliArgs
exit $LASTEXITCODE
