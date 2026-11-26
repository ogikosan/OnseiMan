' Run the OnseiMan recognition server in the background (no window). Keep this file ASCII-only.
' Used by Windows sign-in auto start: wscript.exe "<OnseiMan>\server\start_server_hidden.vbs"
' If the server environment is not ready yet, open server\start_server.bat in a normal window to set it up.
Option Explicit

Dim fso, shell, serverDir, root, logsDir, pythonExe, appPy, logFile, threads, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

serverDir = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(serverDir)
logsDir = root & "\logs"
pythonExe = root & "\.venv_server\Scripts\python.exe"
appPy = root & "\server\app.py"
logFile = logsDir & "\server.log"
shell.CurrentDirectory = root

If Not fso.FileExists(root & "\.venv_server\onseiman-ready.txt") Then
    shell.Run """" & serverDir & "\start_server.bat""", 1, False
    WScript.Quit
End If
If Not fso.FolderExists(logsDir) Then fso.CreateFolder(logsDir)

' Half of the CPU threads, between 2 and 8
threads = 4
On Error Resume Next
threads = CInt(shell.ExpandEnvironmentStrings("%NUMBER_OF_PROCESSORS%")) \ 2
On Error GoTo 0
If threads < 2 Then threads = 2
If threads > 8 Then threads = 8

cmd = "cmd.exe /c """"" & pythonExe & """ """ & appPy & """ --host 0.0.0.0 --port 8765 --threads " & threads & " > """ & logFile & """ 2>&1"""
shell.Run cmd, 0, False
