@echo off
REM Convenience wrapper: restart the web_test demo server (see restart_server.ps1).
REM Usage: restart_server.bat [port]
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_server.ps1" %*
exit /b %ERRORLEVEL%
