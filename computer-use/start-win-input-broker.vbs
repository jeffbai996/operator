Option Explicit

' Start one input broker from Explorer's interactive context. Task Scheduler's
' InteractiveToken process can still be assigned a window station that rejects
' SetCursorPos; an Explorer child runs on the logged-in user's input desktop.
Dim shell, fso, installDir, brokerDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
installDir = fso.GetParentFolderName(WScript.ScriptFullName)
brokerDir = shell.ExpandEnvironmentStrings("%TEMP%") & "\operator-input-broker"
command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " & _
          Chr(34) & installDir & "\win_input.ps1" & Chr(34) & " -BrokerDir " & _
          Chr(34) & brokerDir & Chr(34) & " -IdleSeconds 300"
shell.Run command, 0, True
