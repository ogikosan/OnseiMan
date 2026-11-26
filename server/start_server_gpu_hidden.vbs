' Run the OnseiMan GPU recognition server (Haas-ja in WSL2) in the background (no window). Keep this file ASCII-only.
' Used by Windows sign-in auto start: wscript.exe "<OnseiMan>\server\start_server_gpu_hidden.vbs"
' The server runs inside the onseiman-gpu Linux on 127.0.0.1:8766. server\relay.py listens on port 8765
' on Windows and passes connections to it, so other PCs can reach it like the CPU server.
' If the setup is not finished yet, open server_gpu.bat in a normal window.
Option Explicit

Dim fso, shell, serverDir, root, logsDir, logFile, relayPy, lin, Q, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Q = Chr(34)

serverDir = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(serverDir)
logsDir = root & "\logs"
logFile = logsDir & "\server_gpu.log"
relayPy = root & "\.venv_relay\Scripts\pythonw.exe"
shell.CurrentDirectory = root

If Not fso.FileExists(root & "\.venv_relay\onseiman-gpu-ready.txt") Then
    shell.Run Q & root & "\server_gpu.bat" & Q, 1, False
    WScript.Quit
End If
If Not fso.FolderExists(logsDir) Then fso.CreateFolder(logsDir)

' D:\Tools\OnseiMan -> /mnt/d/Tools/OnseiMan
lin = "/mnt/" & LCase(Left(root, 1)) & Replace(Mid(root, 3), "\", "/")

cmd = "cmd.exe /c " & Q & "wsl.exe -d onseiman-gpu -u root -- bash " & Q & lin & "/server/wsl/run_haas.sh" & Q & " " & Q & lin & Q & " > " & Q & logFile & Q & " 2>&1" & Q
shell.Run cmd, 0, False
shell.Run Q & relayPy & Q & " " & Q & root & "\server\relay.py" & Q, 0, False
