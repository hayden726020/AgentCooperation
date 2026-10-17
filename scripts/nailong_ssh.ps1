#requires -Version 5.1
<#
.SYNOPSIS
    Windows-side Nailong SSH launcher.

.DESCRIPTION
    Reads a small, fixed set of system facts from an Ubuntu host, plays a
    local terminal animation, and then opens a normal interactive SSH session.
    The remote probe is read-only and does not install or persist anything.
#>

[CmdletBinding(PositionalBinding = $true)]
param(
    [Parameter(Position = 0)]
    [string] $Server,
    [string] $User = 'spcuteag',
    [int] $Port = 22,
    [string] $IdentityFile,
    [switch] $NoColor,
    [switch] $NoAnimation,
    [switch] $Preview,
    [switch] $DryRun
)

Set-StrictMode -Version 2.0
$script:NailongDotSourced = $MyInvocation.InvocationName -eq '.'
$script:NailongEsc = [char]27

function Test-NailongTarget {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $User,
        [Parameter(Mandatory = $true)][int] $Port,
        [string] $IdentityFile
    )

    if ([string]::IsNullOrWhiteSpace($Server) -or
        $Server.StartsWith('-') -or
        $Server -match '[\s\x00-\x1f\x7f]' -or
        $Server -notmatch '^(?:[A-Za-z0-9][A-Za-z0-9._:%-]*|\[[A-Fa-f0-9:.%]+\])$') {
        throw 'Invalid server. Use a hostname, SSH alias, IPv4 address, or IPv6 address without spaces.'
    }
    if ($User -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$') {
        throw 'Invalid user. Linux user names may contain letters, digits, underscore, dot, and hyphen.'
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw 'Port must be between 1 and 65535.'
    }
    if ($IdentityFile -and -not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
        throw 'Identity file was not found.'
    }
}

function Get-NailongSshArguments {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $User,
        [Parameter(Mandatory = $true)][int] $Port,
        [string] $IdentityFile
    )

    Test-NailongTarget -Server $Server -User $User -Port $Port -IdentityFile $IdentityFile
    $destination = '{0}@{1}' -f $User, $Server
    $authentication = @()
    if ($IdentityFile) {
        $authentication += '-i'
        $authentication += (Resolve-Path -LiteralPath $IdentityFile).Path
    }

    [pscustomobject]@{
        Destination = $destination
        Probe = @('-T', '-p', [string]$Port) + $authentication +
            @(
                '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=5',
                '-o', 'ServerAliveCountMax=2',
                '--', $destination, 'timeout 8 sh -s'
            )
        Interactive = @('-t', '-p', [string]$Port) + $authentication +
            @('--', $destination)
    }
}

function Get-NailongRemoteProbeScript {
    @'
#!/bin/sh
set -u

clean_value() {
    printf '%s' "$1" | tr '\r\n' '  ' | cut -c 1-160
}

hostname_value=$(hostname 2>/dev/null || printf 'N/A')
os_value='Ubuntu'
if [ -r /etc/os-release ]; then
    parsed_os=$(awk '
        /^PRETTY_NAME=/ {
            sub(/^[^=]*=/, "")
            if ($0 ~ /^".*"$/) { sub(/^"/, ""); sub(/"$/, "") }
            print
            exit
        }' /etc/os-release 2>/dev/null || true)
    [ -n "$parsed_os" ] && os_value=$parsed_os
fi
uptime_value=$(uptime -p 2>/dev/null | sed 's/^up //' || true)
[ -n "$uptime_value" ] || uptime_value='N/A'
load_value=$(awk '{ printf "%s %s %s", $1, $2, $3 }' /proc/loadavg 2>/dev/null || true)
[ -n "$load_value" ] || load_value='N/A'
memory_value=$(awk '
    /^MemTotal:/ { total=$2 }
    /^MemAvailable:/ { available=$2 }
    END {
        if (total > 0) {
            used=total-available
            printf "%.0f/%.0f MiB (%.0f%%)", used/1024, total/1024, used*100/total
        }
    }' /proc/meminfo 2>/dev/null || true)
[ -n "$memory_value" ] || memory_value='N/A'
disk_value=$(df -Pm / 2>/dev/null | awk 'NR==2 { printf "%s/%s MiB (%s)", $3, $2, $5 }' || true)
[ -n "$disk_value" ] || disk_value='N/A'
ip_value=$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '
    BEGIN { count=0 }
    {
        value=tolower($0)
        if (value ~ /^10\./ || value ~ /^192\.168\./ ||
            value ~ /^172\.(1[6-9]|2[0-9]|3[01])\./ ||
            value ~ /^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\./ ||
            value ~ /^(fc|fd|fe80:)/) {
            if (count > 0) printf ", "
            printf "%s", $0
            count++
            if (count == 3) exit
        }
    }' || true)
[ -n "$ip_value" ] || ip_value='N/A'

printf '%s\n' '__NAILONG_BEGIN__'
printf 'HOSTNAME=%s\n' "$(clean_value "$hostname_value")"
printf 'OS=%s\n' "$(clean_value "$os_value")"
printf 'UPTIME=%s\n' "$(clean_value "$uptime_value")"
printf 'LOAD=%s\n' "$(clean_value "$load_value")"
printf 'MEMORY=%s\n' "$(clean_value "$memory_value")"
printf 'DISK=%s\n' "$(clean_value "$disk_value")"
printf 'IP=%s\n' "$(clean_value "$ip_value")"
printf '%s\n' '__NAILONG_END__'
'@
}

function Remove-NailongTerminalControls {
    param([AllowEmptyString()][string] $Value)
    if ($null -eq $Value) { return '' }
    $clean = $Value -replace "\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))", ''
    $clean = $clean -replace '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', ''
    $clean = $clean -replace '[\u061C\u200B-\u200F\u202A-\u202E\u2066-\u2069]', ''
    if ($clean.Length -gt 160) { $clean = $clean.Substring(0, 160) }
    return $clean.Trim()
}

function ConvertFrom-NailongProbeOutput {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]] $Lines)

    $expected = @{
        HOSTNAME = 'Hostname'; OS = 'OS'; UPTIME = 'Uptime'; LOAD = 'Load'
        MEMORY = 'Memory'; DISK = 'Disk'; IP = 'IP'
    }
    $values = @{}
    $inside = $false
    $complete = $false
    foreach ($rawLine in $Lines) {
        $line = [string]$rawLine
        if ($line -eq '__NAILONG_BEGIN__') { $inside = $true; continue }
        if ($inside -and $line -eq '__NAILONG_END__') { $complete = $true; break }
        if (-not $inside) { continue }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) { continue }
        $key = $line.Substring(0, $separator)
        if ($expected.ContainsKey($key)) {
            $values[$expected[$key]] = Remove-NailongTerminalControls $line.Substring($separator + 1)
        }
    }
    if (-not $complete) { throw 'The server probe did not return a complete Nailong data block.' }
    foreach ($property in $expected.Values) {
        if (-not $values.ContainsKey($property) -or [string]::IsNullOrWhiteSpace($values[$property])) {
            $values[$property] = 'N/A'
        }
    }
    [pscustomobject]@{
        Hostname = $values.Hostname; OS = $values.OS; Uptime = $values.Uptime
        Load = $values.Load; Memory = $values.Memory; Disk = $values.Disk; IP = $values.IP
    }
}

function Get-NailongLayout {
    param([int] $Width)
    if ($Width -ge 90) { return 'Wide' }
    if ($Width -ge 60) { return 'Compact' }
    return 'Mini'
}

function Get-NailongTerminalWidth {
    try {
        $width = [int]$Host.UI.RawUI.WindowSize.Width
        if ($width -gt 0) { return [Math]::Max(10, $width) }
    } catch { }
    return 80
}

function Test-NailongColorSupport {
    param([switch] $NoColor, [bool] $IsOutputRedirected, [bool] $SupportsVirtualTerminal)
    if ($NoColor -or $IsOutputRedirected -or -not $SupportsVirtualTerminal) { return $false }
    if ($env:NO_COLOR -or $env:TERM -eq 'dumb') { return $false }
    return $true
}

function Test-NailongInteractiveConsole {
    try { return -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected }
    catch { return $false }
}

function Get-NailongTitleLines {
    param(
        [ValidateSet('Wide', 'Compact', 'Mini')][string] $Layout,
        [int] $Width = 80
    )
    if ($Layout -eq 'Wide') {
        return @(
            '  SSSS   PPPP    CCCC  U   U  TTTTT  EEEEE   AAA    GGGG',
            ' S       P   P  C      U   U    T    E      A   A  G',
            '  SSS    PPPP   C      U   U    T    EEEE   AAAAA  G  GG',
            '     S   P      C      U   U    T    E      A   A  G   G',
            ' SSSS    P       CCCC   UUU     T    EEEEE  A   A   GGG',
            '',
            '                       SPCUTEAG的奶服'
        )
    }
    if ($Layout -eq 'Mini' -and $Width -lt 22) {
        return @('SPCUTEAG', '的奶服')
    }
    return @('=== SPCUTEAG的奶服 ===')
}

function Get-NailongArtLines {
    param(
        [ValidateSet('Wide', 'Compact', 'Mini')][string] $Layout,
        [ValidateSet('Appear', 'LaughLeft', 'LaughRight', 'Final')][string] $Pose
    )
    if ($Layout -eq 'Mini') {
        if ($Pose -eq 'Appear') {
            return @('     .------.', '    (  o  o  )', '     \  --  /', "      '----'")
        }
        return @('     .------.', '    (  >  <  )', '    (  \____/ )', '     \_(____)_/  HA!')
    }
    if ($Layout -eq 'Compact') {
        if ($Pose -eq 'Appear') {
            return @(
                '        .----------.', '      /    o    o    \',
                '     |       --       |', '     |     .----.      |',
                '      \___/      \____/'
            )
        }
        $lean = if ($Pose -eq 'LaughLeft') { ' ' } elseif ($Pose -eq 'LaughRight') { '   ' } else { '  ' }
        return @(
            ($lean + '      .----------.'), ($lean + '    /     >  <     \'),
            ($lean + '   |     .----.     |'), ($lean + '   |    / HAHA \    |'),
            ($lean + '   |  _/\______/\_  |'), ($lean + '    \(____/  \____)/')
        )
    }
    if ($Pose -eq 'Appear') {
        return @(
            '                     .------------.',
            "                  .-'    o    o    '-.",
            '                /          --          \',
            '               |        .------.        |',
            '               |       /        \       |',
            '                \_____/          \_____/'
        )
    }
    $leanWide = if ($Pose -eq 'LaughLeft') { ' ' } elseif ($Pose -eq 'LaughRight') { '     ' } else { '   ' }
    return @(
        ($leanWide + '                .------------.'),
        ($leanWide + "             .-'     >  <     '-."),
        ($leanWide + '           /        .------.       \'),
        ($leanWide + '          |        / HA HA! \       |'),
        ($leanWide + '          |       / \______/ \      |'),
        ($leanWide + '          |    __/            \__   |'),
        ($leanWide + '           \  /   \__________/   \ /'),
        ($leanWide + '            \(______ /  \ ______)/'),
        ($leanWide + '                    \____/')
    )
}

function Get-NailongCharacterWidth {
    param([char] $Character)
    $code = [int]$Character
    if (($code -ge 0x1100 -and $code -le 0x115F) -or
        $code -eq 0x2329 -or $code -eq 0x232A -or
        ($code -ge 0x2E80 -and $code -le 0xA4CF -and $code -ne 0x303F) -or
        ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
        ($code -ge 0xF900 -and $code -le 0xFAFF) -or
        ($code -ge 0xFE10 -and $code -le 0xFE19) -or
        ($code -ge 0xFE30 -and $code -le 0xFE6F) -or
        ($code -ge 0xFF00 -and $code -le 0xFF60) -or
        ($code -ge 0xFFE0 -and $code -le 0xFFE6)) {
        return 2
    }
    return 1
}

function Get-NailongDisplayWidth {
    param([AllowEmptyString()][string] $Text)
    $total = 0
    foreach ($character in $Text.ToCharArray()) {
        $total += Get-NailongCharacterWidth -Character $character
    }
    return $total
}

function Limit-NailongLine {
    param([string] $Text, [int] $Width)
    if ((Get-NailongDisplayWidth -Text $Text) -le $Width) { return $Text }
    if ($Width -le 3) { return '.' * [Math]::Max(0, $Width) }
    $budget = $Width - 3
    $used = 0
    $builder = New-Object Text.StringBuilder
    foreach ($character in $Text.ToCharArray()) {
        $characterWidth = Get-NailongCharacterWidth -Character $character
        if ($used + $characterWidth -gt $budget) { break }
        [void]$builder.Append($character)
        $used += $characterWidth
    }
    return $builder.ToString() + '...'
}

function Get-NailongStatusLines {
    param(
        [Parameter(Mandatory = $true)] $Facts,
        [ValidateSet('Wide', 'Compact', 'Mini')][string] $Layout,
        [int] $Width
    )
    $lines = @('', '奶龙已上线，开始工作', '')
    if ($Layout -eq 'Mini') {
        $lines += @(
            ('主机: {0}' -f $Facts.Hostname), ('系统: {0}' -f $Facts.OS),
            ('运行: {0}' -f $Facts.Uptime), ('负载: {0}' -f $Facts.Load),
            ('内存: {0}' -f $Facts.Memory), ('磁盘: {0}' -f $Facts.Disk),
            ('内网: {0}' -f $Facts.IP)
        )
    } else {
        $lines += @(
            ('  主机名  {0}' -f $Facts.Hostname), ('  系统    {0}' -f $Facts.OS),
            ('  运行    {0}' -f $Facts.Uptime), ('  负载    {0}' -f $Facts.Load),
            ('  内存    {0}' -f $Facts.Memory), ('  根分区  {0}' -f $Facts.Disk),
            ('  内网 IP {0}' -f $Facts.IP)
        )
    }
    return @($lines | ForEach-Object { Limit-NailongLine -Text $_ -Width $Width })
}

function Get-NailongStaticView {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] $Facts,
        [Parameter(Mandatory = $true)][int] $Width,
        [switch] $UseColor
    )
    $layout = Get-NailongLayout -Width $Width
    $title = @(Get-NailongTitleLines -Layout $layout -Width $Width |
        ForEach-Object { Limit-NailongLine -Text $_ -Width $Width })
    $art = @(Get-NailongArtLines -Layout $layout -Pose Final |
        ForEach-Object { Limit-NailongLine -Text $_ -Width $Width })
    $status = @(Get-NailongStatusLines -Facts $Facts -Layout $layout -Width $Width)
    if (-not $UseColor) {
        return (($title + '' + $art + $status) -join [Environment]::NewLine)
    }
    $reset = "$script:NailongEsc[0m"
    $cyan = "$script:NailongEsc[96m"
    $yellow = "$script:NailongEsc[93m"
    $green = "$script:NailongEsc[92m"
    $heading = ($status[0..2] -join [Environment]::NewLine)
    $details = ($status[3..($status.Count - 1)] -join [Environment]::NewLine)
    return (($cyan + ($title -join [Environment]::NewLine) + $reset) +
        [Environment]::NewLine + [Environment]::NewLine +
        ($yellow + ($art -join [Environment]::NewLine) + $reset) +
        [Environment]::NewLine + ($green + $heading + $reset) +
        [Environment]::NewLine + $details)
}

function Get-NailongAnimationPlan {
    param([ValidateSet('Wide', 'Compact', 'Mini')][string] $Layout)
    @(
        [pscustomobject]@{ Stage = 'Appear'; Pose = 'Appear'; DurationMs = 300 },
        [pscustomobject]@{ Stage = 'BellyLaugh'; Pose = 'LaughLeft'; DurationMs = 300 },
        [pscustomobject]@{ Stage = 'BellyLaugh'; Pose = 'LaughRight'; DurationMs = 300 },
        [pscustomobject]@{ Stage = 'BellyLaugh'; Pose = 'LaughLeft'; DurationMs = 300 },
        [pscustomobject]@{ Stage = 'BellyLaugh'; Pose = 'LaughRight'; DurationMs = 250 },
        [pscustomobject]@{ Stage = 'Final'; Pose = 'Final'; DurationMs = 250 }
    )
}

function Wait-NailongFrame {
    param([int] $DurationMs)
    $elapsed = 0
    while ($elapsed -lt $DurationMs) {
        try {
            if ([Console]::KeyAvailable) {
                do { [void][Console]::ReadKey($true) } while ([Console]::KeyAvailable)
                return $true
            }
        } catch { }
        $slice = [Math]::Min(25, $DurationMs - $elapsed)
        Start-Sleep -Milliseconds $slice
        $elapsed += $slice
    }
    return $false
}

function Write-NailongFrame {
    param(
        [Parameter(Mandatory = $true)] $Facts,
        [ValidateSet('Wide', 'Compact', 'Mini')][string] $Layout,
        [ValidateSet('Appear', 'LaughLeft', 'LaughRight', 'Final')][string] $Pose,
        [int] $Width,
        [switch] $UseColor,
        [switch] $IncludeStatus
    )
    if ($IncludeStatus) {
        [Console]::WriteLine((Get-NailongStaticView -Facts $Facts -Width $Width -UseColor:$UseColor))
        return
    }
    $title = @(Get-NailongTitleLines -Layout $Layout -Width $Width |
        ForEach-Object { Limit-NailongLine -Text $_ -Width $Width }) -join [Environment]::NewLine
    $art = @(Get-NailongArtLines -Layout $Layout -Pose $Pose |
        ForEach-Object { Limit-NailongLine -Text $_ -Width $Width }) -join [Environment]::NewLine
    if ($UseColor) {
        [Console]::WriteLine("$script:NailongEsc[96m$title$script:NailongEsc[0m")
        [Console]::WriteLine()
        [Console]::WriteLine("$script:NailongEsc[93m$art$script:NailongEsc[0m")
    } else {
        [Console]::WriteLine($title)
        [Console]::WriteLine()
        [Console]::WriteLine($art)
    }
}

function Show-NailongAnimation {
    param([Parameter(Mandatory = $true)] $Facts, [switch] $NoColor, [switch] $NoAnimation)
    $width = Get-NailongTerminalWidth
    $layout = Get-NailongLayout -Width $width
    $redirected = $true
    try { $redirected = [Console]::IsOutputRedirected } catch { }
    $supportsVirtualTerminal = $false
    try { $supportsVirtualTerminal = [bool]$Host.UI.SupportsVirtualTerminal } catch { }
    $useColor = Test-NailongColorSupport -NoColor:$NoColor `
        -IsOutputRedirected:$redirected -SupportsVirtualTerminal:$supportsVirtualTerminal
    $interactive = Test-NailongInteractiveConsole
    if ($NoAnimation -or -not $interactive -or $env:TERM -eq 'dumb') {
        [Console]::WriteLine((Get-NailongStaticView -Facts $Facts -Width $width -UseColor:$useColor))
        return
    }
    $oldCursorVisible = $true
    try {
        try { $oldCursorVisible = [Console]::CursorVisible; [Console]::CursorVisible = $false } catch { }
        $skipped = $false
        foreach ($frame in @(Get-NailongAnimationPlan -Layout $layout)) {
            Clear-Host
            Write-NailongFrame -Facts $Facts -Layout $layout -Pose $frame.Pose `
                -Width $width -UseColor:$useColor -IncludeStatus:($frame.Stage -eq 'Final')
            if (Wait-NailongFrame -DurationMs $frame.DurationMs) { $skipped = $true; break }
        }
        if ($skipped) {
            Clear-Host
            Write-NailongFrame -Facts $Facts -Layout $layout -Pose Final `
                -Width $width -UseColor:$useColor -IncludeStatus
        }
    } finally {
        try { [Console]::CursorVisible = $oldCursorVisible; [Console]::ResetColor() } catch { }
    }
}

function Resolve-NailongSshExecutable {
    if ($env:WINDIR) {
        foreach ($systemPath in @(
            (Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'),
            (Join-Path $env:WINDIR 'Sysnative\OpenSSH\ssh.exe')
        )) {
            if (Test-Path -LiteralPath $systemPath -PathType Leaf) { return $systemPath }
        }
    }
    foreach ($name in @('ssh.exe', 'ssh')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    throw 'OpenSSH client was not found. Enable the Windows OpenSSH Client feature first.'
}

function Invoke-NailongProbe {
    param([Parameter(Mandatory = $true)][string] $SshExecutable,
        [Parameter(Mandatory = $true)][string[]] $Arguments)
    $probeText = Get-NailongRemoteProbeScript
    if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
        $PSNativeCommandUseErrorActionPreference = $false
    }
    $output = @($probeText | & $SshExecutable @Arguments 2>&1)
    [pscustomobject]@{ ExitCode = [int]$LASTEXITCODE; Output = $output }
}

function Invoke-NailongInteractiveSession {
    param([Parameter(Mandatory = $true)][string] $SshExecutable,
        [Parameter(Mandatory = $true)][string[]] $Arguments)
    & $SshExecutable @Arguments
    $script:NailongInteractiveExitCode = [int]$LASTEXITCODE
}

function Invoke-NailongSsh {
    [CmdletBinding()]
    param(
        [string] $Server, [string] $User = 'spcuteag', [int] $Port = 22,
        [string] $IdentityFile, [switch] $NoColor, [switch] $NoAnimation,
        [switch] $Preview, [switch] $DryRun
    )
    if ($Preview) {
        $previewFacts = [pscustomobject]@{
            Hostname = 'milk-host'; OS = 'Ubuntu 24.04 LTS'; Uptime = '2 days, 3 hours'
            Load = '0.10 0.20 0.30'; Memory = '512/2048 MiB (25%)'
            Disk = '1024/4096 MiB (25%)'; IP = '192.168.1.8'
        }
        Show-NailongAnimation -Facts $previewFacts -NoColor:$NoColor -NoAnimation:$NoAnimation
        $global:LASTEXITCODE = 0
        return
    }
    if ([string]::IsNullOrWhiteSpace($Server)) {
        throw 'Server is required. Example: nailong-ssh 192.168.1.20'
    }
    $sshArguments = Get-NailongSshArguments -Server $Server -User $User `
        -Port $Port -IdentityFile $IdentityFile
    if ($DryRun) {
        return [pscustomobject]@{
            destination = $sshArguments.Destination; port = $Port
            probe_arguments = @($sshArguments.Probe)
            interactive_arguments = @($sshArguments.Interactive)
        }
    }
    $sshExecutable = Resolve-NailongSshExecutable
    try {
        $probe = Invoke-NailongProbe -SshExecutable $sshExecutable -Arguments $sshArguments.Probe
    } catch {
        $probe = [pscustomobject]@{ ExitCode = 1; Output = @() }
    }
    if ($probe.ExitCode -eq 0) {
        try {
            $facts = ConvertFrom-NailongProbeOutput -Lines $probe.Output
            Show-NailongAnimation -Facts $facts -NoColor:$NoColor -NoAnimation:$NoAnimation
        } catch {
            Write-Warning ('无法解析服务器状态，将直接进入 SSH：{0}' -f $_.Exception.Message)
        }
    } else {
        Write-Warning '状态采集未成功，将直接尝试正常 SSH 登录。'
    }
    Invoke-NailongInteractiveSession -SshExecutable $sshExecutable `
        -Arguments $sshArguments.Interactive
    $global:LASTEXITCODE = $script:NailongInteractiveExitCode
}

if (-not $script:NailongDotSourced) {
    $invokeParameters = @{
        Server = $Server; User = $User; Port = $Port; IdentityFile = $IdentityFile
        NoColor = $NoColor; NoAnimation = $NoAnimation; Preview = $Preview; DryRun = $DryRun
    }
    Invoke-NailongSsh @invokeParameters
}
