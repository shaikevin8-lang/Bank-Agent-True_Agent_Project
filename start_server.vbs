' Start the Bank-Agent web_test server completely hidden (no console window).
' Nothing to close by accident; the server keeps running after this script exits.
'
' Usage:
'   double-click this file, or run:  wscript.exe start_server.vbs
'   (start_local.ps1 does the same thing plus the DB container check)
'
' Configuration:
'   BANK_AGENT_DB_URL is read from the process environment, or from the
'   KEY=VALUE line of the repo-root .env file. If neither provides it the
'   script prints an error and quits with exit code 1 (no fallback default).
'
' Stop:  Stop-Process -Id <pid of python.exe running web_test\server.py> -Force

Option Explicit

Dim root, serverDir, python, logFile, errLogFile, cmd, sh, fso
Dim dbUrl, envFile

' 以脚本自身所在目录作为项目根目录，避免硬编码本机绝对路径。
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
serverDir = root & "\Struct_of_Agent"
python = root & "\.venv\Scripts\python.exe"
logFile = serverDir & "\web_test\server.log"
errLogFile = serverDir & "\web_test\server.err.log"
If Not fso.FileExists(python) Then
    WScript.Echo "Python venv not found: " & python
    WScript.Quit 1
End If

Set sh = CreateObject("WScript.Shell")

' Resolve the connection string. Priority: process environment, then repo-root .env.
' No default credentials are ever hardcoded here; a missing value is a hard failure.
dbUrl = "" & sh.Environment("PROCESS")("BANK_AGENT_DB_URL")
dbUrl = Trim(dbUrl)

envFile = root & "\.env"

If Len(dbUrl) = 0 And fso.FileExists(envFile) Then
    dbUrl = ReadEnvValue(ReadTextFileUtf8(envFile))
End If

If Len(dbUrl) = 0 Then
    WScript.Echo "ERROR: BANK_AGENT_DB_URL is not configured - server not started."
    WScript.Echo ""
    WScript.Echo "Set it in one of these two ways, then run this script again:"
    WScript.Echo "  1) environment variable, e.g.:"
    WScript.Echo "       setx BANK_AGENT_DB_URL ""postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/bank_agent_legacy"""
    WScript.Echo "  2) copy .env.example to .env in the repo root and fill in BANK_AGENT_DB_URL="
    WScript.Echo ""
    WScript.Echo "Repo root checked: " & root
    WScript.Echo ".env file: " & envFile
    WScript.Quit 1
End If

' Child process environment: keeps credentials out of the command line and logs.
sh.Environment("PROCESS")("BANK_AGENT_DB_URL") = dbUrl
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
sh.Environment("PROCESS")("PYTHONUTF8") = "1"
sh.CurrentDirectory = serverDir

' None of these paths contain spaces, so no quoting is needed.
cmd = "cmd /c " & python & " web_test\server.py >> " & logFile & " 2>> " & errLogFile

' Window style 0 = hidden, bWaitOnReturn False = do not block.
sh.Run cmd, 0, False

' ---------------------------------------------------------------------------
' Helpers
' ---------------------------------------------------------------------------

' Read a UTF-8 text file (falling back to ANSI when ADODB is unavailable).
' Scripting.FileSystemObject can only read ANSI or UTF-16, and its ANSI decoder
' mangles multi-byte characters badly enough to swallow line breaks, so the
' preferred path goes through ADODB.Stream with an explicit utf-8 charset.
Function ReadTextFileUtf8(path)
    Dim s, objStream, objFso, objTs, buf
    s = ""
    On Error Resume Next
    Set objStream = CreateObject("ADODB.Stream")
    objStream.Type = 2                ' adTypeText
    objStream.Charset = "utf-8"
    objStream.Open
    objStream.LoadFromFile path
    s = objStream.ReadText(-1)        ' adReadAll
    objStream.Close
    If Err.Number <> 0 Then
        s = ""
        Err.Clear
    End If
    On Error GoTo 0
    If Left(s, 1) = ChrW(65279) Then s = Mid(s, 2)     ' strip UTF-8 BOM
    If Len(s) = 0 Then
        ' Fallback for the (rare) case ADODB is missing: fine for ASCII-only files.
        Set objFso = CreateObject("Scripting.FileSystemObject")
        Set objTs = objFso.OpenTextFile(path, 1)
        buf = objTs.ReadAll
        objTs.Close
        s = buf
        If Left(s, 1) = ChrW(65279) Then s = Mid(s, 2)
    End If
    ReadTextFileUtf8 = s
End Function

' Find BANK_AGENT_DB_URL in dotenv-style text: skip blank lines and "#" comments,
' split on the first "=", trim, and strip one optional pair of surrounding quotes.
Function ReadEnvValue(fileText)
    Dim arr, i, ln, p, k, v
    ReadEnvValue = ""
    arr = Split(fileText, vbLf)
    For i = 0 To UBound(arr)
        ln = Trim(Replace(arr(i), vbCr, ""))
        If Len(ln) > 0 Then
            If Left(ln, 1) <> "#" Then
                p = InStr(ln, "=")
                If p > 1 Then
                    k = Trim(Left(ln, p - 1))
                    If LCase(k) = "bank_agent_db_url" Then
                        v = Trim(Mid(ln, p + 1))
                        If Len(v) >= 2 Then
                            If (Left(v, 1) = """" And Right(v, 1) = """") Or (Left(v, 1) = "'" And Right(v, 1) = "'") Then
                                v = Mid(v, 2, Len(v) - 2)
                            End If
                        End If
                        ReadEnvValue = Trim(v)
                        Exit Function
                    End If
                End If
            End If
        End If
    Next
End Function
