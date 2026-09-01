<#
    phone-watchdog.ps1  -  keep the phone drives honest.

    phone.ps1 mounts a phone in the foreground on purpose: the console IS the
    mount, and closing it takes the drive away. That is right for "I want this
    drive for the next hour". This is the opposite case. The drives should
    just be there, and should come back on their own when a phone drops off
    wifi or Termux's sshd restarts.

    Each tick, per phone:

        not mounted, phone answers  ->  mount it
        mounted, phone gone         ->  unmount it

    The unmount matters as much as the mount. When a mount loses its phone,
    WinFsp keeps the drive letter sitting there and every read against it
    blocks, so Explorer freezes on the folder and a scan stalls. A letter that
    is gone is honest; a letter that hangs is not.

    "Answers" means TCP connects and sshd sends its banner. That is the exact
    question worth asking, is this phone on the network with sshd up, and both
    answers arrive in well under a second. A directory listing would prove
    more than is being asked and can take tens of seconds on a phone whose
    session setup is slow, which reads as "gone" and unmounts a live drive.

    Liveness is judged at the phone, never by reading the mounted letter. A
    read against a wedged mount blocks in the kernel, and the thread doing it
    cannot be abandoned cleanly, so a watchdog that reads the drive is a
    watchdog that can hang. The cost of that choice: a mount that is stuck
    while its phone is still reachable is not detected, only one whose phone
    has gone.

    Nothing here is a second source of truth. Host and port come from rclone's
    config, letters and labels from the phones.json phone.ps1 writes, so
    'phone.ps1 add' is all it takes for a phone to be watched.

    Timings, all tunable below:

        tick interval           5 min, set on the scheduled task, not here
        healthy tick            a few seconds, mostly 3s per absent phone
        probe timeout           3s, tried twice when a mount is at stake
        unmount after           1 failed tick, so within one interval
        mount, letter appears   waited for up to 60s
        mount stuck with no letter    restarted after 10 min
        log rotation            at 5 MB, one .old kept

    One failed tick is enough because the probe cannot really be wrong: it is
    retried once, and a phone that refuses TCP twice is off the network.

        powershell -File phone-watchdog.ps1                    # all phones
        powershell -File phone-watchdog.ps1 -Only <name>,<name>
        powershell -File phone-watchdog.ps1 -WhatIf            # say, don't do

    Task Scheduler runs it through phone-watchdog-hidden.vbs, which is what
    stops a console flashing up every tick. To register, from this folder:

        $vbs = Join-Path $PWD 'phone-watchdog-hidden.vbs'
        $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
        $every5 = New-TimeSpan -Minutes 5

        # Two triggers. At logon covers every boot from here on, but on its
        # own the task sits idle until the next logon, which is not obvious
        # when registering it from an already logged-in session. The dated one
        # starts the repetition now.
        #
        # No -RepetitionDuration on either: an absent duration is what Task
        # Scheduler reads as "forever". [TimeSpan]::MaxValue looks like it
        # should say the same thing and is rejected as out of range.
        $atLogon = New-ScheduledTaskTrigger -AtLogOn
        $atLogon.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval $every5).Repetition
        $fromNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval $every5

        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -MultipleInstances IgnoreNew

        Register-ScheduledTask -TaskName 'Phone Mount Watchdog' `
            -Action $action -Trigger $atLogon, $fromNow -Settings $settings -Force

    Stopping it is two steps, and neither is "End" in Task Scheduler: the
    mounts are launched detached, so they are not in the task's process tree
    and outlive it. Disable first, or the next tick puts them back.

        Disable-ScheduledTask -TaskName 'Phone Mount Watchdog'
        powershell -File phone.ps1 unmount -Name all
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    # Empty means the file beside this script.
    [string]$PhonesFile,
    [string]$Log,

    # Empty means every phone in phones.json.
    [string[]]$Only = @(),

    # rclone's own default is unlimited, which quietly fills the disk as large
    # files are read through the mount. Matches phone.ps1.
    [string]$CacheSize = '10G',

    [int]$ProbeTimeoutMs     = 3000,
    [int]$DeadStrikes        = 1,
    [int]$MountWaitSeconds   = 60,
    [int]$WedgedAfterMinutes = 10,
    [int]$MaxLogMB           = 5
)

$ErrorActionPreference = 'Stop'

# Not param defaults: $PSScriptRoot is empty there under Windows PowerShell
# 5.1, which is what Task Scheduler runs.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $PhonesFile) { $PhonesFile = Join-Path $here 'phones.json' }
if (-not $Log)        { $Log        = Join-Path $here 'logs\phone-watchdog.log' }

$LogDir    = Split-Path $Log -Parent
$StateFile = Join-Path $LogDir 'phone-watchdog-state.json'


function Write-Line([string]$Message) {
    Add-Content -Path $Log -Value ("{0}  {1}" -f (Get-Date -Format 's'), $Message)
}

function Get-RclonePath {
    # Steps past the scoop shim deliberately: the shim spawns the real binary
    # as a child, leaving two processes per drive and making "stop the rclone
    # holding this letter" ambiguous. 'current' survives a scoop update.
    $direct = Join-Path $env:USERPROFILE 'scoop\apps\rclone\current\rclone.exe'
    if (Test-Path $direct) { return $direct }

    $cmd = Get-Command rclone -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    throw 'rclone was not found in the scoop apps folder or on PATH.'
}

function Get-SftpEndpoints($Rclone) {
    # One call, kept in memory: the dump also carries the stored passwords, so
    # it must never reach the log.
    $table = @{}
    foreach ($r in (& $Rclone config dump 2>$null | ConvertFrom-Json).PSObject.Properties) {
        if ($r.Value.type -ne 'sftp') { continue }
        $table[$r.Name] = @{
            host = $r.Value.host
            port = if ($r.Value.port) { [int]$r.Value.port } else { 22 }
        }
    }
    return $table
}

function Test-Mounted([string]$Letter) {
    # Not Test-Path: that reads the volume, which is the thing that hangs.
    # GetDrives only lists letters, so it cannot block.
    $root = $Letter.TrimEnd('\') + '\'
    return [bool]([IO.DriveInfo]::GetDrives() | Where-Object { $_.Name -eq $root })
}

function Test-Reachable($Endpoint, [switch]$Retry) {
    # On the network with sshd up. Retried only where a wrong answer costs a
    # mount; with nothing mounted a dropped packet costs one tick of waiting.
    foreach ($attempt in 1..$(if ($Retry) { 2 } else { 1 })) {
        if ($attempt -gt 1) { Start-Sleep -Milliseconds 500 }

        $client = [Net.Sockets.TcpClient]::new()
        try {
            if (-not $client.ConnectAsync($Endpoint.host, $Endpoint.port).Wait($ProbeTimeoutMs)) { continue }
            $stream = $client.GetStream()
            $stream.ReadTimeout = $ProbeTimeoutMs
            $buffer = [byte[]]::new(64)
            $read = $stream.Read($buffer, 0, $buffer.Length)
            if ([Text.Encoding]::ASCII.GetString($buffer, 0, $read) -like 'SSH-*') { return $true }
        } catch {
        } finally {
            $client.Dispose()
        }
    }
    return $false
}

function Get-MountProcess([string]$Letter) {
    # A mount that is still retrying has no drive letter yet but is very much
    # running. Without this check every tick would start another one.
    Get-CimInstance Win32_Process -Filter "Name='rclone.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -match [regex]::Escape(" $Letter ") -and
            $_.CommandLine -match 'mount'
        } |
        Select-Object -First 1
}

function Read-State {
    if (-not (Test-Path $StateFile)) { return @{} }
    try {
        $table = @{}
        (Get-Content $StateFile -Raw | ConvertFrom-Json).PSObject.Properties |
            ForEach-Object { $table[$_.Name] = [int]$_.Value }
        return $table
    } catch {
        # A corrupt state file costs one phone an extra tick of grace, which is
        # not worth failing the run over.
        return @{}
    }
}

function Dismount([string]$Key, [string]$Letter) {
    <#
      Stop the rclone holding this letter so the drive goes away cleanly.

      Anything still in the VFS write cache is lost, which sounds worse than
      it is: the phone is unreachable, so that data was not going to be
      uploaded from this mount anyway. It stays in rclone's cache directory.
    #>
    if (-not $PSCmdlet.ShouldProcess("$Key ($Letter)", 'unmount')) { return $false }

    $proc = Get-MountProcess $Letter
    if ($proc) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Test-Mounted $Letter)) { Write-Line "$Key : $Letter released."; return $true }
    }

    Write-Line "$Key : asked rclone to stop but $Letter is still there, may need a manual unmount."
    return $false
}


# --- Setup ---------------------------------------------------------------

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt $MaxLogMB * 1MB)) {
    Move-Item $Log "$Log.old" -Force
}

if (-not (Test-Path $PhonesFile)) {
    Write-Line "phones.json not found at $PhonesFile, nothing to watch."
    exit 0
}

$rclone    = Get-RclonePath
$endpoints = Get-SftpEndpoints $rclone

$phones = @{}
(Get-Content $PhonesFile -Raw | ConvertFrom-Json).PSObject.Properties |
    ForEach-Object { $phones[$_.Name] = $_.Value }

$keys = @($phones.Keys | Sort-Object)
if ($Only.Count) { $keys = $keys | Where-Object { $Only -contains $_ } }

$state = Read-State


# --- The tick ------------------------------------------------------------

foreach ($key in $keys) {
    $p        = $phones[$key]
    $letter   = $p.letter
    $endpoint = $endpoints[$key]

    if (-not $endpoint) {
        Write-Line "$key : no sftp remote of that name in rclone's config, skipping."
        continue
    }

    $mounted = Test-Mounted $letter
    $alive   = Test-Reachable $endpoint -Retry:$mounted

    # ---- Mounted: still worth having? ----
    if ($mounted) {
        if ($alive) {
            if ($state[$key]) {
                Write-Line "$key : answering again, cleared $($state[$key]) strike(s)."
                $state.Remove($key)
            }
            continue
        }

        $strikes = [int]$state[$key] + 1
        $state[$key] = $strikes

        if ($strikes -lt $DeadStrikes) {
            Write-Line "$key : $letter is mounted but the phone is off the network (strike $strikes/$DeadStrikes)."
            continue
        }

        Write-Line "$key : $letter is mounted but the phone is off the network, unmounting."
        if (Dismount $key $letter) { $state.Remove($key) }
        continue
    }

    # ---- Not mounted: should it be? ----
    $state.Remove($key)

    $existing = Get-MountProcess $letter
    if ($existing) {
        # Running with no drive letter: still connecting, or wedged against a
        # phone that went away mid-session. Only the second deserves a kill.
        $since = (Get-Date).AddMinutes(-$WedgedAfterMinutes)
        if (-not $existing.CreationDate -or $existing.CreationDate -ge $since) { continue }

        Write-Line "$key : mount $($existing.ProcessId) held $letter with no drive for ${WedgedAfterMinutes}+ min, restarting it."
        if ($PSCmdlet.ShouldProcess("$key ($letter)", 'stop wedged rclone mount')) {
            Stop-Process -Id $existing.ProcessId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }

    # Phone off or away from the network is expected, not an error.
    if (-not $alive) { continue }

    Write-Line "$key : reachable and $letter is free, mounting."
    if (-not $PSCmdlet.ShouldProcess("$key -> $letter", 'mount')) { continue }

    # Its own hidden console, so the mount outlives this script. A child
    # sharing this console would be torn down when the watchdog exits.
    Start-Process -FilePath $rclone -WindowStyle Hidden -ArgumentList @(
        'mount', "${key}:$($p.path)", $letter
        '--volname', $p.volname
        '--vfs-cache-mode', 'full'
        '--vfs-cache-max-size', $CacheSize
        '--log-file', (Join-Path $LogDir "rclone-mount-$key.log")
        '--log-level', 'INFO'
    ) | Out-Null

    # Confirm rather than assume: a mount that fails immediately should say so.
    # The wait is generous because first contact pays a full login, which is
    # tens of seconds on a phone with slow session setup.
    $up = $false
    for ($i = 0; $i -lt $MountWaitSeconds * 2; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Mounted $letter) { $up = $true; break }
    }

    if ($up) { Write-Line "$key : $letter is up as $($p.label)." }
    else     { Write-Line "$key : mount started but $letter did not appear within ${MountWaitSeconds}s, see rclone-mount-$key.log." }
}

if (-not $WhatIfPreference) {
    ($state | ConvertTo-Json -Depth 3) | Set-Content $StateFile -Encoding utf8
}

# A phone being away is the normal case, but it can leave rclone's non-zero
# exit behind. Without this the task's Last Run Result reads 0x1 and looks
# broken.
exit 0
