#requires -Version 7.3
param(
    [string] $Workspace = (Split-Path $PSScriptRoot -Parent),
    [string] $TaskFile,
    [string] $AgyExecutable,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
# Script-scoped preferences preserve multiline text and quotes in native argv.
$PSNativeCommandArgumentPassing = 'Standard'
$PSNativeCommandUseErrorActionPreference = $false

foreach ($value in @($Workspace, $TaskFile, $AgyExecutable)) {
    if ($null -ne $value -and $value.Contains([char] 0)) {
        throw 'Launcher parameters cannot contain NUL characters.'
    }
}
if ([string]::IsNullOrWhiteSpace($Workspace)) {
    throw 'Workspace must name an existing filesystem directory.'
}
$workspaceItem = Get-Item -LiteralPath $Workspace -ErrorAction Stop
if ($workspaceItem.PSProvider.Name -ne 'FileSystem' -or -not $workspaceItem.PSIsContainer) {
    throw 'Workspace must name an existing filesystem directory.'
}
$workspacePath = $workspaceItem.FullName

$taskText = $null
if ($PSBoundParameters.ContainsKey('TaskFile')) {
    if ([string]::IsNullOrWhiteSpace($TaskFile)) {
        throw 'TaskFile must name a nonempty UTF-8 file.'
    }
    $taskItem = Get-Item -LiteralPath $TaskFile -ErrorAction Stop
    if ($taskItem.PSProvider.Name -ne 'FileSystem' -or $taskItem.PSIsContainer) {
        throw 'TaskFile must name a nonempty UTF-8 file.'
    }
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    $taskText = [IO.File]::ReadAllText($taskItem.FullName, $strictUtf8)
    if ([string]::IsNullOrWhiteSpace($taskText) -or $taskText.Contains([char] 0)) {
        throw 'TaskFile must contain nonempty UTF-8 text without NUL characters.'
    }
}

if ($PSBoundParameters.ContainsKey('AgyExecutable')) {
    if ([string]::IsNullOrWhiteSpace($AgyExecutable)) {
        throw 'AgyExecutable must name an existing native .exe file.'
    }
    if (Test-Path -LiteralPath $AgyExecutable -PathType Leaf) {
        $exeItem = Get-Item -LiteralPath $AgyExecutable
    } else {
        $application = Get-Command -Name $AgyExecutable -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $application) {
            throw 'AgyExecutable must name an existing native .exe file.'
        }
        $exeItem = Get-Item -LiteralPath $application.Source
    }
} else {
    $application = Get-Command -Name 'agy.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $application) {
        $exeItem = Get-Item -LiteralPath $application.Source
    } elseif (Test-Path -LiteralPath 'E:\Tools\AntigravityCLI\bin\agy.exe' -PathType Leaf) {
        $exeItem = Get-Item -LiteralPath 'E:\Tools\AntigravityCLI\bin\agy.exe'
    } else {
        throw 'Native agy.exe was not found. Specify its path with -AgyExecutable.'
    }
}
if ($exeItem.PSProvider.Name -ne 'FileSystem' -or $exeItem.PSIsContainer -or $exeItem.Name -ine 'agy.exe') {
    throw 'AgyExecutable must name an existing native agy.exe file; other executables, scripts and command strings are not supported.'
}
$executablePath = $exeItem.FullName

# Leave the native login home and keyring untouched. Only inspect provider selection.
$nativeHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $env:HOME }
if ([string]::IsNullOrWhiteSpace($nativeHome)) {
    throw 'Native user profile directory is unavailable.'
}
$settingsPath = Join-Path $nativeHome '.gemini\antigravity-cli\settings.json'
if (Test-Path -LiteralPath $settingsPath) {
    try {
        $nativeSettings = Get-Content -LiteralPath $settingsPath -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $nativeSettings -or $nativeSettings -isnot [pscustomobject]) {
            throw 'Invalid settings object.'
        }
        $providerProperty = $nativeSettings.PSObject.Properties['modelProvider']
        $provider = if ($null -ne $providerProperty) { $providerProperty.Value } else { $null }
        if ($null -ne $provider -and $provider -isnot [string]) {
            throw 'Invalid provider selection.'
        }
    } catch {
        throw 'Native Antigravity settings could not be read safely. Check settings.json locally; its contents were not printed.'
    }
    if (-not [string]::IsNullOrWhiteSpace($provider) -and $provider -ine 'antigravity') {
        throw 'Native CLI settings select an API or unsupported provider. Select Antigravity in the native CLI first; this launcher does not change authentication settings.'
    }
}

[string[]] $nativeArguments = @('--model', 'gemini-3.8-flash-high', '--effort', 'high')
if ($null -ne $taskText) {
    $nativeArguments += @('--prompt-interactive', $taskText)
}
if ($DryRun) {
    [ordered]@{
        executable = $executablePath
        workspace = $workspacePath
        model = 'gemini-3.8-flash-high'
        effort = 'high'
        task_present = ($null -ne $taskText)
        task_chars = $(if ($null -ne $taskText) { $taskText.Length } else { 0 })
        mode = 'native-interactive'
    } | ConvertTo-Json
    exit 0
}

$previousLocation = Get-Location
$apiVariables = @('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_GEMINI_BASE_URL')
$previousEnvironment = @{}
foreach ($name in $apiVariables) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    foreach ($name in $apiVariables) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    Set-Location -LiteralPath $workspacePath
    # Invoke the native foreground application directly, with literal argument values.
    # Native permissions and interactive approvals remain under the CLI's control.
    & $executablePath @nativeArguments
    $nativeExitCode = $LASTEXITCODE
} finally {
    try {
        Set-Location -LiteralPath $previousLocation.Path
    } finally {
        foreach ($name in $apiVariables) {
            [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
    }
}
exit $nativeExitCode
