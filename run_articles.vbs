' Articles Downloader — robust launcher (full Python path, no console, logged).
Option Explicit
Dim WshShell, fso, q, appDir, py, logPath
q = Chr(34)
appDir = "C:\Users\chkam\OneDrive\Desktop\BrandFinder\Utilities"
logPath = appDir & "\launch.log"
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = appDir
py = "C:\Users\chkam\AppData\Local\Programs\Python\Python314\python.exe"
If Not fso.FileExists(py) Then py = "python.exe"
WshShell.Run "cmd /c " & q & q & py & q & " -u Articles_v2.py > " & q & logPath & q & " 2>&1" & q, 0, False
