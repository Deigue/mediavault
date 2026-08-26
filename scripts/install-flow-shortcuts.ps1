<#
install-flow-shortcuts.ps1 - make the dashboard controls findable in Flow.

Flow Launcher has no Task Scheduler plugin, but its Program plugin indexes
the Start Menu by default. Putting four shortcuts there means typing
"mediavault" into Flow lists start, stop, restart and status with no plugin
to install and no Flow settings to change. They show up in the Windows Start
Menu search too, for free.

    powershell -File scripts\install-flow-shortcuts.ps1
    powershell -File scripts\install-flow-shortcuts.ps1 -Uninstall

Shortcuts go in the per-user Start Menu, so no administrator rights are
needed and nothing outside this profile is touched.
#>

[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$repo    = Split-Path -Parent $PSScriptRoot
$script  = Join-Path $PSScriptRoot 'dashboard-service.ps1'
$folder  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\MediaVault'

# Named so that typing "mediavault" in Flow brings up all four together, and
# the verb is the second word so "mv re" narrows straight to Restart.
$shortcuts = @(
    @{ Name = 'MediaVault Dashboard';         Action = 'start';   Open = $true
       Desc = 'Start the MediaVault dashboard and open it in the browser' }
    @{ Name = 'MediaVault Restart Dashboard'; Action = 'restart'; Open = $false
       Desc = 'Restart the dashboard so Python changes take effect' }
    @{ Name = 'MediaVault Stop Dashboard';    Action = 'stop';    Open = $false
       Desc = 'Stop the MediaVault dashboard' }
    @{ Name = 'MediaVault Dashboard Status';  Action = 'status';  Open = $false
       Desc = 'Is the MediaVault dashboard running?' }
)

if ($Uninstall) {
    if (Test-Path $folder) {
        Remove-Item $folder -Recurse -Force
        Write-Output "Removed $folder"
    } else {
        Write-Output 'Nothing to remove.'
    }
    return
}

if (-not (Test-Path $script)) { throw "Cannot find $script" }
if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }

# powershell.exe rather than pwsh: it is always present, and these only use
# cmdlets that Windows PowerShell has.
$shell = New-Object -ComObject WScript.Shell
foreach ($s in $shortcuts) {
    $lnk = $shell.CreateShortcut((Join-Path $folder "$($s.Name).lnk"))
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" " +
            "-Action $($s.Action) -Gui"
    if ($s.Open) { $args += ' -Open' }
    $lnk.Arguments        = $args
    $lnk.WorkingDirectory = $repo
    $lnk.Description      = $s.Desc
    # Python's icon, so the four of them are recognisable in Flow's list.
    $lnk.IconLocation     = "$env:SystemRoot\System32\shell32.dll,25"
    $lnk.Save()
    Write-Output "Created $($s.Name)"
}

Write-Output ''
Write-Output "Shortcuts are in $folder"
Write-Output 'Flow indexes the Start Menu, but only rescans periodically. To see'
Write-Output 'them straight away, open Flow settings and press "Reload Plugin Data",'
Write-Output 'or just wait for the next refresh.'
