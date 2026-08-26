<#
install-phone-shortcuts.ps1 - make the phone mount controls findable in Flow.

Same idea as install-flow-shortcuts.ps1: Flow Launcher's Program plugin
indexes the Start Menu, so shortcuts there are searchable with no plugin to
install and no Flow settings to change.

    powershell -File scripts\install-phone-shortcuts.ps1
    powershell -File scripts\install-phone-shortcuts.ps1 -Uninstall

These are named verb first - "Mount Phone", not "MediaVault Mount Phone" -
because Flow matches from the start of the name. Typing "mount" should land
on the thing that mounts, without a prefix in the way. That is the opposite
convention to the dashboard shortcuts, where the shared prefix is the point:
those are four views of one thing and belong together in the list, while
these are four different verbs someone reaches for individually.

None of them hide their window. The mount window IS the mount - rclone has to
keep running for the drive to exist, so closing the window is how you unmount
- and the rest ask questions that need somewhere to be answered.

Shortcuts go in the per-user Start Menu, so no administrator rights are
needed and nothing outside this profile is touched.
#>

[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$repo   = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'phone.ps1'
$folder = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\MediaVault'

# Keep is $true where the window must outlive the script: anything that asks
# questions or prints something worth reading. Mount does not need it - that
# window stays up for as long as the drive does, and should close when the
# drive goes away.
$shortcuts = @(
    @{ Name = 'Mount Phone';   Action = 'mount';   Keep = $false
       Desc = 'Mount a phone as a drive letter. Close the window to unmount.' }
    @{ Name = 'Unmount Phone'; Action = 'unmount'; Keep = $false
       Desc = 'Unmount a phone and stop rclone' }
    @{ Name = 'Add Phone';     Action = 'add';     Keep = $true
       Desc = 'Set up a new phone - address, login, drive letter and volume name' }
    @{ Name = 'Remove Phone';  Action = 'remove';  Keep = $true
       Desc = 'Forget a phone and delete its rclone remote' }
    @{ Name = 'List Phones';   Action = 'list';    Keep = $true
       Desc = 'Which phones are set up, and which are mounted right now' }
)

if ($Uninstall) {
    $removed = 0
    foreach ($s in $shortcuts) {
        $path = Join-Path $folder "$($s.Name).lnk"
        if (Test-Path $path) { Remove-Item $path -Force; $removed++ }
    }
    Write-Output "Removed $removed shortcut(s) from $folder"
    # The folder is shared with the dashboard shortcuts, so it is only worth
    # removing once nothing else is left in it.
    if ((Test-Path $folder) -and -not (Get-ChildItem $folder)) {
        Remove-Item $folder -Force
        Write-Output "Removed the now-empty $folder"
    }
    return
}

if (-not (Test-Path $script)) { throw "Cannot find $script" }
if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }

# powershell.exe rather than pwsh: it is always present, and phone.ps1 only
# uses cmdlets that Windows PowerShell has.
$shell = New-Object -ComObject WScript.Shell
foreach ($s in $shortcuts) {
    $lnk = $shell.CreateShortcut((Join-Path $folder "$($s.Name).lnk"))
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

    $args = '-NoProfile -ExecutionPolicy Bypass'
    if ($s.Keep) { $args += ' -NoExit' }
    $args += " -File `"$script`" -Action $($s.Action)"

    $lnk.Arguments        = $args
    $lnk.WorkingDirectory = $repo
    $lnk.Description      = $s.Desc
    # A drive icon, so these read as drive controls rather than scripts.
    $lnk.IconLocation     = "$env:SystemRoot\System32\shell32.dll,8"
    $lnk.Save()
    Write-Output "Created $($s.Name)"
}

Write-Output ''
Write-Output "Shortcuts are in $folder"
Write-Output 'Flow indexes the Start Menu, but only rescans periodically. To see'
Write-Output 'them straight away, open Flow settings and press "Reload Plugin Data",'
Write-Output 'or just wait for the next refresh.'
