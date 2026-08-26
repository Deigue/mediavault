<#
phone.ps1 - mount a phone's storage as a drive letter, on demand.

A phone running an SSH server (Termux + openssh) can be mounted over SFTP as
a real drive letter, which is what lets MediaVault treat it like any other
drive. Nothing here runs in the background: the mount exists only while you
have asked for it, so there is no scanning, no cache housekeeping and no CPU
cost at all the rest of the time.

    powershell -File scripts\phone.ps1 add
    powershell -File scripts\phone.ps1 mount   -Name pixel
    powershell -File scripts\phone.ps1 unmount -Name pixel
    powershell -File scripts\phone.ps1 list
    powershell -File scripts\phone.ps1 remove  -Name pixel

Mounting deliberately holds the console window it was started from. rclone
has to keep running for the drive to exist, so the window is the honest
representation of that: while it is open the drive is there, and closing it
takes the drive away. That also means there is always an obvious way to stop
it without hunting for a process.

Credentials are not stored here. "add" hands the password to rclone, which
keeps it obscured in its own config alongside every other remote. This script
only records the things rclone does not care about - which letter to use and
what to call the volume - in phones.json beside it.
#>

[CmdletBinding()]
param(
    [ValidateSet('add', 'mount', 'unmount', 'list', 'remove')]
    [string]$Action = 'list',

    # Which phone to act on. Optional when only one is configured.
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

function Resolve-Phone($Phones, [string]$Wanted) {
    <#
      The phone to act on. Naming one is only required when there is more
      than one, which keeps the common single-phone case to a bare verb.
    #>
    if ($Wanted) {
        if (-not $Phones.ContainsKey($Wanted)) {
            throw "No phone called '$Wanted'. Run 'list' to see what is set up."
        }
        return $Wanted
    }

    $names = @($Phones.Keys)
    if ($names.Count -eq 0) { throw "No phones set up yet. Run 'add' first." }
    if ($names.Count -eq 1) { return $names[0] }

    Write-Output 'Which phone?'
    for ($i = 0; $i -lt $names.Count; $i++) {
        Write-Output ("  [{0}] {1}  ({2})" -f ($i + 1), $names[$i], $Phones[$names[$i]].letter)
    }
    $pick = Read-Host 'Number'
    $index = 0
    if (-not [int]::TryParse($pick, [ref]$index) -or $index -lt 1 -or $index -gt $names.Count) {
        throw 'Not one of the choices.'
    }
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
        Write-Output '  Required.'
    }
}


function Invoke-Add {
    $phones = Read-Phones
    $rclone = Get-RclonePath

    Write-Output 'Adding a phone. Everything is asked once and remembered.'
    Write-Output ''

    $name = Read-RequiredHost 'Short name (no spaces, e.g. pixel)'
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


function Invoke-Mount {
    $phones = Read-Phones
    $key = Resolve-Phone $phones $Name
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


function Invoke-Unmount {
    $phones = Read-Phones
    $key = Resolve-Phone $phones $Name
    $p = $phones[$key]

    $proc = Get-MountProcess $p.letter
    if (-not $proc) {
        Write-Output "$key does not appear to be mounted."
        Start-Sleep -Seconds 2
        return
    }

    Stop-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-Mounted $p.letter)) {
            Write-Output "$key unmounted from $($p.letter)."
            Start-Sleep -Seconds 2
            return
        }
        Start-Sleep -Milliseconds 250
    }
    Write-Output "Asked rclone to stop, but $($p.letter) is still there. Close its window."
    Start-Sleep -Seconds 4
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
