' Opens Delta without a console window. Used by the Desktop and Start menu shortcuts.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
sh.Run """" & root & "\.venv\Scripts\python.exe"" -m delta app", 0, False
