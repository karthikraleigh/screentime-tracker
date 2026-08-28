' Launches the screen time tracker silently (no console window) using pythonw.exe.
' Used for auto-start at login. Safe to double-click manually too.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = scriptDir & "\tracker_server.py"

Set shell = CreateObject("WScript.Shell")
shell.Run "pythonw """ & scriptPath & """", 0, False
