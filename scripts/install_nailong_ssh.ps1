#requires -Version 5.1
<#
.SYNOPSIS
    Installs or uninstalls the nailong-ssh command for the current user.
#>

[CmdletBinding()]
param(
    [string] $ProfilePath = $PROFILE.CurrentUserAllHosts,
    [string] $InstallRoot = (Join-Path (Split-Path $PROFILE.CurrentUserAllHosts -Parent) 'NailongSsh'),
    [switch] $Uninstall
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$beginMarker = '# >>> nailong-ssh >>>'
$endMarker = '# <<< nailong-ssh <<<'
$blockPattern = '(?ms)^# >>> nailong-ssh >>>\r?\n.*?^# <<< nailong-ssh <<<\r?\n?'
$sourceScript = Join-Path $PSScriptRoot 'nailong_ssh.ps1'
$installedScript = Join-Path $InstallRoot 'nailong_ssh.ps1'
$utf8Bom = New-Object System.Text.UTF8Encoding($true)

function Read-ProfileText {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    return [IO.File]::ReadAllText($Path)
}

function Write-ProfileText {
    param([string] $Path, [string] $Text)
    $parent = Split-Path $Path -Parent
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        [void](New-Item -ItemType Directory -Path $parent -Force)
    }
    $temporary = Join-Path $parent ('.{0}.nailong-ssh.{1}.tmp' -f (Split-Path $Path -Leaf), $PID)
    try {
        [IO.File]::WriteAllText($temporary, $Text, $utf8Bom)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporary, $Path, ($Path + '.nailong-ssh.bak'), $true)
        } else {
            Move-Item -LiteralPath $temporary -Destination $Path
        }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

$profileText = Read-ProfileText -Path $ProfilePath
$hasBegin = $profileText.Contains($beginMarker)
$hasEnd = $profileText.Contains($endMarker)
if ($hasBegin -xor $hasEnd) {
    throw 'The PowerShell profile contains an incomplete nailong-ssh marker block. Repair it manually before continuing.'
}

if ($Uninstall) {
    if ($hasBegin) {
        $updated = [regex]::Replace($profileText, $blockPattern, '')
        Write-ProfileText -Path $ProfilePath -Text $updated
    }
    if (Test-Path -LiteralPath $installedScript -PathType Leaf) {
        Remove-Item -LiteralPath $installedScript -Force
    }
    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
        $remaining = @(Get-ChildItem -LiteralPath $InstallRoot -Force)
        if ($remaining.Count -eq 0) { Remove-Item -LiteralPath $InstallRoot -Force }
    }
    Write-Host 'nailong-ssh 已从当前 PowerShell 用户配置中卸载。' -ForegroundColor Green
    return
}

if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
    throw 'The nailong_ssh.ps1 source file is missing beside the installer.'
}
if (-not (Test-Path -LiteralPath $InstallRoot)) {
    [void](New-Item -ItemType Directory -Path $InstallRoot -Force)
}
Copy-Item -LiteralPath $sourceScript -Destination $installedScript -Force

$escapedPath = $installedScript.Replace("'", "''")
$profileBlock = @"
$beginMarker
function global:nailong-ssh {
    & '$escapedPath' @args
}
$endMarker
"@
$withoutOldBlock = [regex]::Replace($profileText, $blockPattern, '').TrimEnd("`r", "`n")
$updatedProfile = if ($withoutOldBlock) {
    $withoutOldBlock + [Environment]::NewLine + [Environment]::NewLine +
        $profileBlock + [Environment]::NewLine
} else {
    $profileBlock + [Environment]::NewLine
}
if ($updatedProfile -ne $profileText) {
    Write-ProfileText -Path $ProfilePath -Text $updatedProfile
}

Write-Host 'nailong-ssh 已安装到当前 PowerShell 用户。' -ForegroundColor Green
Write-Host '重新打开 PowerShell 后运行：nailong-ssh <服务器地址>' -ForegroundColor Cyan
