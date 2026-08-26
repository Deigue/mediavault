<#
mount-phone.ps1 - keep a phone's rclone mount up.

rclone mount does not survive its remote disappearing. When the phone drops
off wifi the SFTP connection dies and rclone exits outright - the process is
gone, the drive letter with it, and nothing brings either back. Reconnecting
the phone is not enough: sshd is listening again and "rclone about" answers,
but there is no longer anything mounting it. That is the gap this fills.

    powershell -File scripts\mount-phone.ps1
    powershell -File scripts\mount-phone.ps1 -Letter Q: -Remote phone2: -VolName \\PHONE-02\PHO-128GB-02

It runs rclone in the foreground and restarts it whenever it exits, so the
drive comes back on its own once the phone is reachable again. Run it from a
scheduled task at logon rather than rclone directly - and start it through
run-hidden.vbs, so there is no console window for anyone to close. Closing
one would kill this supervisor and take the drive with it.

    wscript.exe scripts\run-hidden.vbs scripts\mount-phone.ps1

Started that way there is nothing on screen, so everything it would have
printed goes to the log beside this script instead. To stop it:

    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like '*mount-phone.ps1*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

The wait between attempts backs off, because a phone that is out of the house
would otherwise have this retrying every few seconds all day for nothing. A
mount that stayed up a while before dying is treated as a healthy one that
just lost its phone, so the backoff resets and it reconnects promptly.

Every default below is a placeholder. Pass the real remote, letter and volume
name as arguments, or edit them here for the machine this runs on.
#>

[CmdletBinding()]
param(
    # The rclone remote and path to mount. Termux serves the whole of internal
    # storage; pointing at the root instead would drag in Android's symlinks.
    [string]$Remote = 'phone:/storage/emulated/0',

    [string]$Letter = 'P:',

    # Shown in Explorer as "PHO-128GB-01 (\\PHONE-01) (P:)". A full UNC path
    # here also implies --network-mode, so that flag is not passed separately.
    [string]$VolName = '\\PHONE-01\PHO-128GB-01',

    # Cap on the local cache. rclone's own default is unlimited, which will
    # quietly fill the disk as large files are read through the mount.
    [string]$CacheSize = '10G',

    # First wait after a failed attempt, and the ceiling it backs off to.
    [int]$RetrySeconds = 10,
    [int]$MaxRetrySeconds = 120,

    # A mount that lasted at least this long counts as having worked, so the
    # next failure starts from the short wait again.
    [int]$HealthySeconds = 60,

    # Where the running commentary goes. Running windowless means there is
    # nowhere else for it, and without it a drive that failed to come back
    # leaves nothing at all to look at. Left empty here and worked out below,
    # because $PSScriptRoot is not populated yet while defaults are being
    # bound - reading it here yields an empty string and Join-Path throws,
    # killing the script before there is any log to record why.
    [string]$LogPath = '',

    # Trim the log once it passes this, so months of a phone coming and
    # going cannot grow a file that never stops.
    [int]$LogMaxBytes = 1MB
)

$ErrorActionPreference = 'Stop'

if (-not $LogPath) {
    # $PSScriptRoot is reliable here in the body, but fall back anyway rather
    # than risk this script's one diagnostic being the thing that fails.
    $root = $PSScriptRoot
    if (-not $root -and $MyInvocation.MyCommand.Path) {
        $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    if (-not $root) { $root = (Get-Location).Path }
    $LogPath = Join-Path $root 'mount-phone.log'
}


function Write-Log([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Output $line
    try {
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch {
        # A log that cannot be written is not a reason to drop the mount.
    }
}

# Start from the tail rather than the beginning, so what is kept is the part
# describing how things got to where they are now.
if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -gt $LogMaxBytes)) {
    try {
        $keep = Get-Content $LogPath -Tail 200
        Set-Content -Path $LogPath -Value $keep -Encoding utf8
    } catch { }
}


function Get-RclonePath {
    <#
      Where rclone is. Looked up rather than hard-coded so this keeps working
      when the package manager updates it and the shim moves.
    #>
    $cmd = Get-Command rclone -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $fallback = Join-Path $env:USERPROFILE 'scoop\shims\rclone.exe'
    if (Test-Path $fallback) { return $fallback }

    throw 'rclone was not found on PATH or in the scoop shims folder.'
}

function Test-LetterInUse([string]$DriveLetter) {
    # Something already holding the letter means either a second copy of this
    # script or a mount left over from before. Either way rclone would fail.
    Test-Path ($DriveLetter.TrimEnd('\') + '\')
}


$rclone = Get-RclonePath
$wait = $RetrySeconds

Write-Log "supervisor started - $Remote on $Letter using $rclone"

while ($true) {
    if (Test-LetterInUse $Letter) {
        Write-Log "$Letter is already in use - waiting rather than fighting over it"
        Start-Sleep -Seconds $MaxRetrySeconds
        continue
    }

    $startedAt = Get-Date
    Write-Log "mounting $Remote on $Letter"

    # Runs in the foreground on purpose: this loop's whole job is to notice
    # when rclone stops, which means waiting on it.
    & $rclone mount $Remote $Letter `
        --volname $VolName `
        --vfs-cache-mode full `
        --vfs-cache-max-size $CacheSize `
        --no-console

    $lasted = (New-TimeSpan -Start $startedAt -End (Get-Date)).TotalSeconds

    if ($lasted -ge $HealthySeconds) {
        # It was working and then the phone went away. Expect it back soon.
        $wait = $RetrySeconds
        Write-Log ("mount ended after {0:N0}s - retrying in {1}s" -f $lasted, $wait)
    } else {
        # Failing immediately means the phone is not reachable at all, so
        # asking again straight away achieves nothing.
        Write-Log ("mount failed after {0:N0}s - retrying in {1}s" -f $lasted, $wait)
    }

    Start-Sleep -Seconds $wait

    if ($lasted -lt $HealthySeconds) {
        $wait = [Math]::Min($wait * 2, $MaxRetrySeconds)
    }
}
