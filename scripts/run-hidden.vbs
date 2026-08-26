' run-hidden.vbs - run a PowerShell script with no window at all.
'
' powershell.exe is a console program, so Windows gives it a console window
' whenever a scheduled task starts it in the interactive session, whatever
' -WindowStyle Hidden claims. That window is not merely untidy: it can be
' closed, and closing it kills the script inside - which for the mount
' supervisor means the drive silently disappears.
'
' wscript.exe is not a console program, so starting the task through this
' allocates no console for anything downstream. The 0 is the window style
' (hidden) and False means do not wait for it to finish.
'
' The task has to stay in the interactive session, so "run whether the user
' is logged on or not" is not an option here - that runs in session 0, and a
' drive letter mapped there is invisible to Explorer and to everything else
' running as you.
'
'   wscript.exe run-hidden.vbs <script.ps1> [arguments...]

Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count = 0 Then
  WScript.Echo "Usage: wscript.exe run-hidden.vbs <script.ps1> [arguments...]"
  WScript.Quit 1
End If

' Anything after the script path is forwarded untouched, so parameters can
' still be passed from the task without the caller nesting quotes.
extra = ""
For i = 1 To WScript.Arguments.Count - 1
  extra = extra & " " & WScript.Arguments(i)
Next

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ _
        & WScript.Arguments(0) & """" & extra

shell.Run command, 0, False
