' Articles Downloader - creates or repairs the desktop shortcut.
'
' Double-click this file once (and again whenever you move this folder).
' It makes an "Articles Downloader" icon on your desktop that starts the app
' silently (no console window). Older Articles Downloader shortcuts on the
' desktop are re-pointed to this folder as well.
'
' Run with /quiet to skip the confirmation message.
Option Explicit

Dim shell, fso, appDir, launcher, icon, desktop, quiet, arg, fixed, name, f, lnk, target

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

quiet = False
For Each arg In WScript.Arguments
  If LCase(arg) = "/quiet" Then quiet = True
Next

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = appDir & "\run_articles.vbs"
icon = appDir & "\articles.ico"

If Not fso.FileExists(launcher) Then
  If Not quiet Then MsgBox "run_articles.vbs was not found in:" & vbCrLf & appDir, vbExclamation, "Articles Downloader"
  WScript.Quit 1
End If

Sub PointAt(path)
  Dim s
  Set s = shell.CreateShortcut(path)
  s.TargetPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"
  s.Arguments = Chr(34) & launcher & Chr(34)
  s.WorkingDirectory = appDir
  s.WindowStyle = 1
  s.Description = "Articles Downloader - search, rank and download research papers"
  If fso.FileExists(icon) Then s.IconLocation = icon & ",0"
  s.Save
End Sub

desktop = shell.SpecialFolders("Desktop")
fixed = 0

' Re-point any older Articles Downloader shortcut already on the desktop.
If fso.FolderExists(desktop) Then
  For Each f In fso.GetFolder(desktop).Files
    If LCase(fso.GetExtensionName(f.Name)) = "lnk" Then
      name = LCase(fso.GetBaseName(f.Name))
      If name <> "articles downloader" And InStr(name, "article") > 0 Then
        Set lnk = shell.CreateShortcut(f.Path)
        target = LCase(lnk.Arguments & " " & lnk.TargetPath)
        If InStr(target, "articles_v2") > 0 Or InStr(target, "run_articles") > 0 Then
          PointAt f.Path
          fixed = fixed + 1
        End If
      End If
    End If
  Next
End If

PointAt desktop & "\Articles Downloader.lnk"

If Not quiet Then
  MsgBox "The Articles Downloader shortcut on your desktop is ready." & vbCrLf & vbCrLf & _
         "It starts the app silently and opens its window." & vbCrLf & _
         "Older shortcuts repaired: " & fixed, vbInformation, "Articles Downloader"
End If
