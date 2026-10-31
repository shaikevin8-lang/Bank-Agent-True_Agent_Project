@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
title 银行智能 Agent - 启动项目
cd /d "%~dp0"
if errorlevel 1 goto failed

set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Use the same local database as web_test/restart_server.ps1.
set "BANK_AGENT_DB_URL=postgresql+psycopg://bank:bank_local@127.0.0.1:5432/bank_agent_legacy"

echo === 启动银行智能 Agent ===
echo.
echo [1/4] 检查 Docker...
where docker >nul 2>&1
if not errorlevel 1 goto docker_found
if not exist "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" (
    echo [错误] 未找到 Docker，请先安装 Docker Desktop，再双击本文件。
    goto failed
)
set "PATH=%ProgramFiles%\Docker\Docker\resources\bin;%PATH%"

:docker_found
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    echo [错误] Docker 未就绪，请先启动 Docker 服务，再重试。
    goto failed
)
echo 正在启动 Docker Desktop，首次启动可能需要约两分钟...
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; Start-Process -FilePath (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe') -WindowStyle Hidden; for ($attempt=0; $attempt -lt 60; $attempt++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { exit 0 }; Start-Sleep -Seconds 2 }; Write-Host '[错误] 等待 Docker 超时，请检查 Docker Desktop。'; exit 1"
if errorlevel 1 goto failed

:docker_ready
echo.
echo [2/4] 准备 Python 环境和依赖...
if exist "%PROJECT_PYTHON%" goto dependencies
for %%P in ("py -3.11" "py -3" "python") do (
    %%~P -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        %%~P -m venv .venv
        if errorlevel 1 goto failed
        goto dependencies
    )
)
echo [错误] 未找到可用的 Python 3.10 或更新版本，请先安装 Python。
goto failed

:dependencies
fc /b "requirements.txt" ".venv\bank-agent-requirements.txt" >nul 2>&1
if not errorlevel 1 goto database
"%PROJECT_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto failed
copy /y "requirements.txt" ".venv\bank-agent-requirements.txt" >nul
if errorlevel 1 goto failed

:database
echo.
echo [3/4] 准备 PostgreSQL 数据库...
docker container inspect bank-agent-pg >nul 2>&1
if not errorlevel 1 goto start_database
docker run -d --name bank-agent-pg ^
    -e POSTGRES_USER=bank ^
    -e POSTGRES_PASSWORD=bank_local ^
    -e POSTGRES_DB=bank_agent_legacy ^
    -p 127.0.0.1:5432:5432 ^
    -v bank-agent-pg-data:/var/lib/postgresql/data ^
    postgres:16.6
if errorlevel 1 goto failed

:start_database
docker start bank-agent-pg >nul
if errorlevel 1 goto failed
powershell.exe -NoProfile -Command "for ($attempt=0; $attempt -lt 30; $attempt++) { docker exec bank-agent-pg pg_isready -h 127.0.0.1 -U bank -d bank_agent_legacy *> $null; if ($LASTEXITCODE -eq 0) { exit 0 }; Start-Sleep -Seconds 2 }; Write-Host '[错误] 数据库未就绪，请运行 docker logs bank-agent-pg 查看原因。'; exit 1"
if errorlevel 1 goto failed
"%PROJECT_PYTHON%" scripts\db.py upgrade
if errorlevel 1 goto failed
"%PROJECT_PYTHON%" scripts\db.py seed
if errorlevel 1 goto failed

echo.
echo [4/4] 后台启动项目...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Struct_of_Agent\web_test\restart_server.ps1" -TimeoutSeconds 60
if errorlevel 1 goto failed
chcp 65001 >nul
echo.
echo 启动成功：http://127.0.0.1:8000
echo 服务已在后台运行，可以关闭本窗口。
start "" "http://127.0.0.1:8000"
exit /b 0

:failed
chcp 65001 >nul
echo.
echo 启动失败，请查看上方错误信息。
echo(Log: "%~dp0Struct_of_Agent\web_test\server.err.log"
pause
exit /b 1
