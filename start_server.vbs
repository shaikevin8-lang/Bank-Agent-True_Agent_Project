' Start the Bank-Agent web_test server completely hidden (no console window).
' Nothing to close by accident; the server keeps running after this script exits.
'
' Usage:
'   double-click this file, or run:  wscript.exe D:\Bank-Agent-legacy\start_server.vbs
'   (start_local.ps1 does the same thing plus the DB container check)
'
' Stop:  Stop-Process -Id <pid of python.exe running web_test\server.py> -Force

Option Explicit

Dim root, serverDir, python, logFile, errLogFile, cmd, sh, fso

root = "D:\Bank-Agent-legacy"
serverDir = root & "\Struct_of_Agent"
python = root & "\.venv\Scripts\python.exe"
logFile = serverDir & "\web_test\server.log"
errLogFile = serverDir & "\web_test\server.err.log"

Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(python) Then
    WScript.Echo "Python venv not found: " & python
    WScript.Quit 1
End If

Set sh = CreateObject("WScript.Shell")
' Child process environment: keeps credentials out of the command line and logs.
sh.Environment("PROCESS")("BANK_AGENT_DB_URL") = "postgresql+psycopg://bank:bank_local@127.0.0.1:5432/bank_agent_legacy"
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
sh.Environment("PROCESS")("PYTHONUTF8") = "1"
sh.CurrentDirectory = serverDir

' None of these paths contain spaces, so no quoting is needed.
cmd = "cmd /c " & python & " web_test\server.py >> " & logFile & " 2>> " & errLogFile

' Window style 0 = hidden, bWaitOnReturn False = do not block.
sh.Run cmd, 0, False
