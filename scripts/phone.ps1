<#
phone.ps1 - mount a phone's storage as a drive letter, on demand.

A phone running an SSH server (Termux + openssh) can be mounted over SFTP as
a real drive letter, which is what lets MediaVault treat it like any other
drive. Nothing here runs in the background: the mount exists only while you
have asked for it, so there is no scanning, no cache housekeeping and no CPU
cost at all the rest of the time.

    powershell -File scripts\phone.ps1 add
    powershell -File scripts\phone.ps1 mount   -Name <name>
    powershell -File scripts\phone.ps1 mount   -Name all
    powershell -File scripts\phone.ps1 unmount -Name <name>
    powershell -File scripts\phone.ps1 unmount -Name all
    powershell -File scripts\phone.ps1 list
    powershell -File scripts\phone.ps1 remove  -Name <name>

Mounting deliberately holds the console it was started from. rclone has to
keep running for the drive to exist, so the console is the honest
representation of that: while it is open the drive is there, and closing it
takes the drive away. That also means there is always an obvious way to stop
it without hunting for a process.

Mounting everything keeps one console per drive and gathers them into tabs of
a single Windows Terminal window, so N drives are not N loose windows. Where
Windows Terminal is missing they are separate windows instead, which is the
same arrangement, laid out worse.

Unmounting does not care which of those it is looking at. It finds the rclone
process holding the drive letter and stops it, and that console then prints
and closes itself. So the tab layout can change freely without unmount
needing to know.

Credentials are not stored here. "add" hands the password to rclone, which
keeps it obscured in its own config alongside every other remote. This script
only records the things rclone does not care about - which letter to use and
what to call the volume - in phones.json beside it.
#>

[CmdletBinding()]
param(
    [ValidateSet('add', 'mount', 'unmount', 'list', 'remove')]
    [string]$Action = 'list',

    # Which phone to act on. Optional when only one is configured. Mount and
    # unmount also take "all".
    [string]$Name = '',

    # Cap on rclone's local cache. Its own default is unlimited, which will
    # quietly fill the disk as large files are read through the mount.
    [string]$CacheSize = '10G'
)

$ErrorActionPreference = 'Stop'

$PhonesFile = Join-Path $PSScriptRoot 'phones.json'


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

function Read-Phones {
    if (-not (Test-Path $PhonesFile)) { return @{} }
    $raw = Get-Content $PhonesFile -Raw
    if (-not $raw.Trim()) { return @{} }

    # PSCustomObject back into a hashtable, so entries can be added and
    # removed without fighting the object model.
    $table = @{}
    ($raw | ConvertFrom-Json).PSObject.Properties |
        ForEach-Object { $table[$_.Name] = $_.Value }
    return $table
}

function Save-Phones($Phones) {
    ($Phones | ConvertTo-Json -Depth 5) | Set-Content $PhonesFile -Encoding utf8
}

function Resolve-Phone($Phones, [string]$Wanted, [switch]$AllowAll) {
    <#
      The phone to act on, or '*' for every one of them. Naming one is only
      required when there is more than one, which keeps the common
      single-phone case to a bare verb.

      The menu is written to the host, not to output. Everything a function
      writes to output is part of what it returns, so a menu printed with
      Write-Output comes back as the answer instead of appearing on screen.
    #>
    if ($Wanted) {
        if ($AllowAll -and $Wanted -eq 'all') { return '*' }
        if (-not $Phones.ContainsKey($Wanted)) {
            throw "No phone called '$Wanted'. Run 'list' to see what is set up."
        }
        return $Wanted
    }

    $names = @($Phones.Keys | Sort-Object)
    if ($names.Count -eq 0) { throw "No phones set up yet. Run 'add' first." }
    if ($names.Count -eq 1) { return $names[0] }

    Write-Host ''
    Write-Host 'Which phone?'
    Write-Host ''
    for ($i = 0; $i -lt $names.Count; $i++) {
        Write-Host ("  [{0}] {1}  ({2})" -f ($i + 1), $names[$i], $Phones[$names[$i]].letter)
    }
    $max = $names.Count
    if ($AllowAll) {
        $max = $names.Count + 1
        Write-Host ("  [{0}] all  (every phone)" -f $max)
    }
    Write-Host ''

    $pick = Read-Host "Enter a number (1-$max)"
    $index = 0
    if (-not [int]::TryParse($pick, [ref]$index) -or $index -lt 1 -or $index -gt $max) {
        throw 'Not one of the choices.'
    }
    if ($AllowAll -and $index -eq $max) { return '*' }
    return $names[$index - 1]
}

function Get-MountProcess([string]$Letter) {
    # Matched on the command line rather than the name: several rclone mounts
    # can be up at once, and only the one holding this letter should be
    # stopped.
    Get-CimInstance Win32_Process -Filter "Name='rclone.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape(" $Letter ") -and $_.CommandLine -match 'mount' } |
        Select-Object -First 1
}

function Test-Mounted([string]$Letter) {
    Test-Path ($Letter.TrimEnd('\') + '\')
}

function Read-RequiredHost([string]$Prompt, [string]$Default = '') {
    while ($true) {
        $shown = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
        $value = (Read-Host $shown).Trim()
        if (-not $value -and $Default) { return $Default }
        if ($value) { return $value }
        Write-Host '  Required.'
    }
}


function Invoke-Add {
    $phones = Read-Phones
    $rclone = Get-RclonePath

    Write-Output 'Adding a phone. Everything is asked once and remembered.'
    Write-Output ''

    $name = Read-RequiredHost 'Short name for this phone (no spaces)'
    $name = $name -replace '\s', ''
    if ($phones.ContainsKey($name)) { throw "A phone called '$name' already exists." }

    $ip   = Read-RequiredHost 'IP address on your network'
    $port = Read-RequiredHost 'SSH port' '8022'
    $user = Read-RequiredHost 'Username (whoami in Termux)'

    $secure = Read-Host 'Password' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $plain) { throw 'A password is required.' }

    $letter = ''
    while ($true) {
        $letter = (Read-RequiredHost 'Drive letter to mount it on (e.g. P:)').ToUpper()
        if ($letter -notmatch '^[A-Z]:$') { Write-Output '  Needs to look like P:'; continue }
        if (Test-Mounted $letter) { Write-Output "  $letter is already in use."; continue }
        break
    }

    $label = Read-RequiredHost 'Volume name shown in Explorer (e.g. PHO-128GB-01)'
    $path  = Read-RequiredHost 'Path on the phone' '/storage/emulated/0'

    # Obscured through a pipe rather than an argument, so the password never
    # appears in a command line where another process could read it.
    $obscured = ($plain | & $rclone obscure -)
    $plain = $null
    if (-not $obscured) { throw 'rclone could not obscure the password.' }

    Write-Output ''
    Write-Output "Creating rclone remote '$name'..."
    # shell_type is deliberately left unset. Forcing it to "none" stops rclone
    # asking the phone how full it is, and the used/total figures are the
    # whole reason for mounting it as a drive rather than copying over SFTP.
    & $rclone config create $name sftp `
        host=$ip port=$port user=$user pass=$obscured `
        known_hosts_file=none | Out-Null

    Write-Output "Testing the connection..."
    $about = & $rclone about "${name}:${path}" 2>&1
    if ($LASTEXITCODE -ne 0) {
        & $rclone config delete $name | Out-Null
        Write-Output ''
        Write-Output 'Could not reach the phone, so nothing was saved:'
        Write-Output "  $about"
        Write-Output ''
        Write-Output 'Check the SSH server is running on the phone and that the'
        Write-Output 'IP, port, username and password are right, then try again.'
        return
    }

    # "about" only asks how full the disk is, and a phone will answer that for
    # a folder it refuses to let anyone read. Listing is the test that matches
    # what mounting actually needs.
    $listing = & $rclone lsd "${name}:${path}" 2>&1
    if ($LASTEXITCODE -ne 0) {
        & $rclone config delete $name | Out-Null
        Write-Output ''
        Write-Output "Logged in, but could not read $path, so nothing was saved:"
        Write-Output "  $listing"
        Write-Output ''
        Write-Output 'The login works, so this is the SSH server having no access to'
        Write-Output 'shared storage rather than anything about the connection.'
        return
    }

    $phones[$name] = [ordered]@{
        letter  = $letter
        label   = $label
        path    = $path
        volname = "\\PHONE-$($name.ToUpper())\$label"
    }
    Save-Phones $phones

    Write-Output ''
    Write-Output $about
    Write-Output ''
    Write-Output "'$name' is set up. Mount it with:"
    Write-Output "    powershell -File scripts\phone.ps1 mount -Name $name"
}


function Get-HostExe {
    # The PowerShell running this script, so the consoles it opens are the
    # same one it was itself started with.
    $exe = (Get-Process -Id $PID).Path
    if ($exe) { return $exe }
    return "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}

function Get-TerminalPath {
    # Windows Terminal, or an empty string when it is not installed.
    $cmd = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $fallback = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wt.exe'
    if (Test-Path $fallback) { return $fallback }
    return ''
}

function Start-MountConsoles($Phones, $Keys) {
    <#
      Start this script again once per phone, each in its own console,
      as tabs of one Windows Terminal window where that is available.

      The command must not contain a semicolon. Windows Terminal splits on
      one wherever it appears, quoted or not, and would read the rest as
      another tab to open.
    #>
    $exe    = Get-HostExe
    $common = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action mount -CacheSize $CacheSize -Name "

    $wt = Get-TerminalPath
    if ($wt) {
        $tabs = foreach ($key in $Keys) {
            "new-tab --title `"$key ($($Phones[$key].letter))`" --suppressApplicationTitle `"$exe`" $common$key"
        }
        try {
            Start-Process -FilePath $wt -ArgumentList ('-w new ' + ($tabs -join ' ; '))
            return
        } catch {
            Write-Output '  Windows Terminal would not start, using separate windows.'
        }
    }

    foreach ($key in $Keys) {
        Start-Process -FilePath $exe -WorkingDirectory $PSScriptRoot -ArgumentList ($common + $key)
    }
}

function Invoke-MountAll($Phones) {
    $todo = @()
    foreach ($key in ($Phones.Keys | Sort-Object)) {
        $p = $Phones[$key]
        if (Test-Mounted $p.letter) {
            Write-Output "  $key skipped, $($p.letter) is already in use."
        } else {
            Write-Output "  $key mounting on $($p.letter) as $($p.label)"
            $todo += $key
        }
    }

    Write-Output ''
    if ($todo.Count -eq 0) {
        Write-Output 'Nothing left to mount.'
        Start-Sleep -Seconds 3
        return
    }

    Start-MountConsoles $Phones $todo
    Write-Output 'Each drive has its own console. Close one to unmount that drive,'
    Write-Output 'or the whole window to unmount all of them.'
    Start-Sleep -Seconds 4
}


function Invoke-Mount {
    $phones = Read-Phones
    $key = Resolve-Phone $phones $Name -AllowAll
    if ($key -eq '*') { Invoke-MountAll $phones; return }

    $p = $phones[$key]
    $rclone = Get-RclonePath

    if (Test-Mounted $p.letter) {
        Write-Output "$($p.letter) is already in use - nothing to do."
        Start-Sleep -Seconds 3
        return
    }

    Write-Output ''
    Write-Output "  Mounting $key on $($p.letter) as $($p.label)"
    Write-Output ''
    Write-Output '  Close this window to unmount and stop rclone.'
    Write-Output '  Nothing runs once it is closed.'
    Write-Output ''

    # Foreground on purpose. rclone must keep running for the drive to exist,
    # so this window is what the drive's existence looks like - and closing it
    # is the plainest possible way to take the drive away again.
    & $rclone mount "$($key):$($p.path)" $p.letter `
        --volname $p.volname `
        --vfs-cache-mode full `
        --vfs-cache-max-size $CacheSize

    Write-Output ''
    Write-Output "$($p.letter) unmounted."
    Start-Sleep -Seconds 2
}


function Dismount-Phone([string]$Key, $Phone) {
    <#
      Stop the rclone holding this drive. Its console notices, says so and
      closes itself, so however the consoles were arranged is not this
      function's problem.
    #>
    $proc = Get-MountProcess $Phone.letter
    if (-not $proc) {
        Write-Output "  $Key is not mounted."
        return
    }

    # A phone that cannot be reached leaves rclone running and retrying with
    # no drive letter ever appearing. That is the same process to stop, but a
    # different thing to say about it.
    $wasUp = Test-Mounted $Phone.letter

    Stop-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-Mounted $Phone.letter)) {
            if ($wasUp) { Write-Output "  $Key unmounted from $($Phone.letter)." }
            else        { Write-Output "  $Key was still trying to connect, stopped." }
            return
        }
        Start-Sleep -Milliseconds 250
    }
    Write-Output "  Asked rclone to stop, but $($Phone.letter) is still there. Close its console."
}


function Close-MountConsoles {
    <#
      Close any console still sitting on a mount action.

      A mount console normally closes itself once its rclone stops, so this
      only catches one that never got as far as starting rclone and has
      nothing left to stop. Only "all" does this: it is the gesture that
      means leave nothing behind.

      Matched on this exact script path, not on the file name. A command line
      is a haystack, and something else that merely mentions this script
      would otherwise be killed along with the real consoles.
    #>
    $me = [regex]::Escape($PSCommandPath)
    $stale = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -match "-File\s+`"?$me`"?\s" -and
            $_.CommandLine -match '\s-Action\s+mount\s'
        }

    foreach ($c in $stale) {
        Stop-Process -Id $c.ProcessId -ErrorAction SilentlyContinue
        Write-Output '  Closed a mount console that had nothing mounted.'
    }
}


function Invoke-Unmount {
    $phones = Read-Phones
    $key = Resolve-Phone $phones $Name -AllowAll

    Write-Output ''
    if ($key -eq '*') {
        foreach ($k in ($phones.Keys | Sort-Object)) { Dismount-Phone $k $phones[$k] }
        # After the consoles have had their own moment to go.
        Start-Sleep -Seconds 3
        Close-MountConsoles
    } else {
        Dismount-Phone $key $phones[$key]
    }
    Start-Sleep -Seconds 3
}


function Invoke-List {
    $phones = Read-Phones
    if ($phones.Count -eq 0) {
        Write-Output "No phones set up. Add one with:"
        Write-Output "    powershell -File scripts\phone.ps1 add"
        return
    }

    Write-Output ''
    Write-Output ('{0,-14} {1,-7} {2,-20} {3}' -f 'NAME', 'DRIVE', 'VOLUME', 'STATUS')
    foreach ($key in ($phones.Keys | Sort-Object)) {
        $p = $phones[$key]
        $status = if (Test-Mounted $p.letter) { 'mounted' } else { 'not mounted' }
        Write-Output ('{0,-14} {1,-7} {2,-20} {3}' -f $key, $p.letter, $p.label, $status)
    }
    Write-Output ''
}


function Invoke-Remove {
    $phones = Read-Phones
    $key = Resolve-Phone $phones $Name
    $p = $phones[$key]

    if (Test-Mounted $p.letter) {
        Write-Output "Unmount $key first."
        return
    }

    $answer = Read-Host "Remove '$key' and its rclone remote? (y/N)"
    if ($answer -ne 'y') { Write-Output 'Left alone.'; return }

    & (Get-RclonePath) config delete $key | Out-Null
    $phones.Remove($key)
    Save-Phones $phones
    Write-Output "'$key' removed."
}


switch ($Action) {
    'add'     { Invoke-Add }
    'mount'   { Invoke-Mount }
    'unmount' { Invoke-Unmount }
    'list'    { Invoke-List }
    'remove'  { Invoke-Remove }
}
