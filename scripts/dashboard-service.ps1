<#
dashboard-service.ps1 - start, stop, restart and check the dashboard.

The dashboard runs as the "MediaVault dashboard" scheduled task, which is
fine for logging in but awkward for everything else: Task Scheduler has no
quick "is it up?" and restarting means clicking through the MMC snap-in.
This wraps the four things worth doing into one command.

    powershell -File scripts\dashboard-service.ps1 status
    powershell -File scripts\dashboard-service.ps1 start
    powershell -File scripts\dashboard-service.ps1 stop
    powershell -File scripts\dashboard-service.ps1 restart

Restart is the one that matters most in practice. The server imports its
Python modules once at startup and runs with debug=False, so no reloader is
watching: any edit to config.py, db.py, scanner.py and the rest only takes
effect after a restart. Templates are the exception - Jinja re-reads those
from disk, which is why dashboard.html changes appear on a page refresh.

Starting goes through the scheduled task rather than launching pythonw here,
so the task definition stays the single source of truth for the interpreter,
the arguments and the working directory.

Stopping does NOT use "schtasks /end". Task Scheduler only knows about a
process it is still tracking, so a dashboard started by hand would survive.
Matching on the command line catches it however it was launched.

-Gui puts the result in a message box instead of the console, which is what
the Start Menu shortcuts use so they do not flash a window.
#>

[CmdletBinding()]
param(
    [ValidateSet('status', 'start', 'stop', 'restart')]
    [string]$Action = 'status',

    # Report through a message box rather than stdout, for shortcut use.
    [switch]$Gui,

    # Open the dashboard in the browser after a successful start.
    [switch]$Open
)

$ErrorActionPreference = 'Stop'

$TaskName = 'MediaVault dashboard'
$Port     = if ($env:MEDIAVAULT_PORT) { [int]$env:MEDIAVAULT_PORT } else { 5151 }
$Url      = "http://127.0.0.1:$Port/"


function Get-DashboardProcess {
    <#
      The running dashboard, or $null.

      Matched on the command line rather than the process name: pythonw.exe
      is a common enough name that killing by name alone could take out
      something unrelated.
    #>
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*dashboard.py*' } |
        Select-Object -First 1
}

function Test-DashboardPort {
    # A live process is not the same as a dashboard you can reach - it may
    # still be starting, or have failed to bind the port.
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-ForPort([int]$Seconds = 15) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardPort) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Stop-Dashboard {
    # Ask politely first. The server holds a SQLite database open, and a
    # forced kill mid-write is how a database ends up needing recovery.
    $proc = Get-DashboardProcess
    if (-not $proc) { return $false }

    $id = $proc.ProcessId
    Stop-Process -Id $id -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $id -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    return $true
}

function Start-Dashboard {
    if (Get-DashboardProcess) { return 'already running' }

    try {
        Start-ScheduledTask -TaskName $TaskName
    } catch {
        return "could not start the '$TaskName' task: $($_.Exception.Message)"
    }

    if (Wait-ForPort) { return 'started' }
    # The task fired but nothing is listening. Almost always a Python error
    # on startup, which pythonw swallows because it has no console.
    return "the task ran but nothing is listening on port $Port"
}

function Get-StatusText {
    $proc = Get-DashboardProcess
    if (-not $proc) { return "MediaVault dashboard: not running." }

    $since = $proc.CreationDate
    $up    = New-TimeSpan -Start $since -End (Get-Date)
    $uptime = if ($up.TotalHours -ge 1) {
        '{0}h {1}m' -f [int]$up.TotalHours, $up.Minutes
    } else {
        '{0}m' -f [int]$up.TotalMinutes
    }

    $reachable = if (Test-DashboardPort) { $Url } else { "not listening on port $Port" }
    "MediaVault dashboard: running.`nPID $($proc.ProcessId), up $uptime.`n$reachable"
}

function Report([string]$Text) {
    if ($Gui) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $Text, 'MediaVault',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    } else {
        Write-Output $Text
    }
}


switch ($Action) {
    'status' {
        Report (Get-StatusText)
    }
    'start' {
        $result = Start-Dashboard
        if ($result -eq 'started' -or $result -eq 'already running') {
            if ($Open) { Start-Process $Url }
            Report (Get-StatusText)
        } else {
            Report "Could not start the dashboard - $result."
        }
    }
    'stop' {
        if (Stop-Dashboard) { Report 'MediaVault dashboard stopped.' }
        else { Report 'MediaVault dashboard was not running.' }
    }
    'restart' {
        [void](Stop-Dashboard)
        # Give the port a moment to come free before the task rebinds it.
        Start-Sleep -Milliseconds 600
        $result = Start-Dashboard
        if ($result -eq 'started' -or $result -eq 'already running') {
            if ($Open) { Start-Process $Url }
            Report (Get-StatusText)
        } else {
            Report "Could not restart the dashboard - $result."
        }
    }
}
