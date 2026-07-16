Option Explicit

Dim shell, fso, scriptDir, startVbs
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
startVbs = fso.BuildPath(scriptDir, "start_ateq_webui_silent.vbs")

shell.Run Chr(34) & startVbs & Chr(34), 0, False
WScript.Sleep 3000
shell.Run "http://127.0.0.1:8001/", 0, False
