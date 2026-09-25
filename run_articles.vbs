' Articles Downloader - silent Windows launcher (no console window).
' Works from any folder: it runs Articles_v2.py located next to this file and
' writes startup errors to launch.log in the same folder.
Option Explicit
Dim sh, fso, q, appDir, py, logPath, candidates, c
q = Chr(34)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
logPath = appDir & "\launch.log"
sh.CurrentDirectory = appDir

' Prefer a local virtual environment, then the Python launcher, then python on PATH.
py = ""
candidates = Array(appDir & "\.venv\Scripts\python.exe", appDir & "\venv\Scripts\python.exe")
For Each c In candidates
  If py = "" And fso.FileExists(c) Then py = c
Next
If py = "" Then
  If sh.Run("cmd /c where py >nul 2>nul", 0, True) = 0 Then
    py = "py"
  Else
    py = "python"
  End If
End If

sh.Run "cmd /c " & q & q & py & q & " -u " & q & appDir & "\Articles_v2.py" & q & " > " & q & logPath & q & " 2>&1" & q, 0, False
