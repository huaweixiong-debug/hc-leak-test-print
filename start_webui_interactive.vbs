Option Explicit

Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\Leak Tester"
shell.Run """D:\Leak Tester\target_start_webui.bat""", 0, False
