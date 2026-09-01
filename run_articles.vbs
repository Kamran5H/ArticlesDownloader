Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\chkam\OneDrive\Desktop\BrandFinder\Utilities"
WshShell.Run "pythonw.exe Articles_v2.py", 0, False
