' Runs phone-watchdog.ps1 with no console window.
' Task Scheduler runs this every few minutes; without it you get a black
' flash on every tick, which is worse than the problem it is solving.
'
' Resolves the script beside itself, so moving the pair needs no edit here.
Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\phone-watchdog.ps1""", 0, False
