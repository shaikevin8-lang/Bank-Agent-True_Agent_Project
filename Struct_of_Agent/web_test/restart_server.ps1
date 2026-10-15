#Requires -Version 5.1
<#
.SYNOPSIS
    重启「银行智能 Agent」web_test 本地测试服务器（银行演示用）。

.DESCRIPTION
    1) 只结束命令行里包含 web_test\server.py 的 python 进程
       （本机还跑着无关的 streamlit 应用，绝不误杀）；
    2) 等待端口释放并确认真的空闲；
    3) 以脱离当前 shell 的方式启动新服务（父 shell 退出后仍存活）；
    4) 轮询 /health/live，直到它报告的 api_version 与源码里的 API_VERSION 一致。

    为什么需要它：页面 index.html 每次请求都从磁盘读取，而路由在进程启动时绑定。
    页面已是新版、后端还是旧进程时，/api/settings/* 只会 404
    （现在会返回 BACKEND_OUTDATED 提示，但根治办法仍然是重启服务）。

.PARAMETER Port
    服务端口，默认 8000。

.PARAMETER TimeoutSeconds
    等待 /health/live 就绪的秒数，默认 20。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\restart_server.ps1

.EXAMPLE
    .\restart_server.bat
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = 'Stop'

# --- 路径：由脚本位置推导，落点就是 D:\Bank-Agent\Struct_of_Agent -------------
$ServerDir  = Split-Path -Parent $PSScriptRoot            # ...\Struct_of_Agent
$ProjectRoot = Split-Path -Parent $ServerDir              # ...\Bank-Agent
$PythonExe  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ServerPy   = Join-Path $ServerDir 'web_test\server.py'
$LogFile    = Join-Path $ServerDir 'web_test\server.log'      # stdout
$ErrLogFile = Join-Path $ServerDir 'web_test\server.err.log'  # stderr（Start-Process 不允许两个流写同一文件）
$BaseUrl    = "http://127.0.0.1:$Port"
$HealthUrl  = "$BaseUrl/health/live"
$Pattern    = 'web_test[\\/]server\.py'                   # 命令行匹配（正/反斜杠都算）

function Get-PortOwner {
    param([int]$LocalPort)
    $conn = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
}

function Get-OurServerProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -match $Pattern -and $_.Name -match '^python'
    }
}

Write-Host "=== 重启 web_test 本地测试服务器（端口 $Port） ===" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "找不到虚拟环境解释器：$PythonExe" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $ServerPy)) {
    Write-Host "找不到服务入口：$ServerPy" -ForegroundColor Red
    exit 1
}

# 源码中的期望版本（避免在脚本里再抄一份常量）
$expectedVersion = $null
$source = Get-Content -LiteralPath $ServerPy -Raw -Encoding UTF8
if ($source -match '(?m)^API_VERSION\s*=\s*"([^"]+)"') { $expectedVersion = $Matches[1] }
Write-Host ("[1/4] 源码 API_VERSION = " + $(if ($expectedVersion) { $expectedVersion } else { '（未解析到）' }))

# --- 1. 结束本项目的旧进程 ------------------------------------------------------
$targets = @(Get-OurServerProcesses)
if ($targets.Count -eq 0) {
    Write-Host "[2/4] 没有发现运行中的 web_test\server.py 进程（可能已停止）。"
} else {
    Write-Host "[2/4] 结束本项目旧进程（仅命令行含 web_test\server.py 的 python）："
    foreach ($p in $targets) {
        Write-Host ("      - PID {0}  {1}" -f $p.ProcessId, $p.CommandLine)
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Host ("        结束 PID {0} 失败：{1}" -f $p.ProcessId, $_.Exception.Message) -ForegroundColor Yellow
        }
    }
    # 等待进程真正退出（最多 5 秒）
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        $left = @(Get-OurServerProcesses)
        if ($left.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
}

# --- 2. 确认端口空闲（绝不误杀非本项目进程） ------------------------------------
$owner = Get-PortOwner $Port
if ($owner -ne 0) {
    $still = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
    if ($still -and $still.CommandLine -and $still.CommandLine -match $Pattern) {
        Write-Host ("      端口仍被本项目 PID {0} 占用，再次结束。" -f $owner) -ForegroundColor Yellow
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        $owner = Get-PortOwner $Port
    }
}
if ($owner -ne 0) {
    Write-Host ("端口 {0} 仍被 PID {1} 占用，且它不是本项目的 server.py；为避免误杀已中止。" -f $Port, $owner) -ForegroundColor Red
    exit 1
}
Write-Host "      端口 $Port 已空闲。"

# --- 3. 脱离当前 shell 启动新服务 -----------------------------------------------
Write-Host "[3/4] 启动新服务（脱离当前 shell，父 shell 退出后仍存活）..."
Write-Host ("      解释器：{0}" -f $PythonExe)
Write-Host ("      工作目录：{0}" -f $ServerDir)
Write-Host ("      日志：{0} / {1}" -f $LogFile, $ErrLogFile)

$proc = Start-Process -FilePath $PythonExe `
    -ArgumentList 'web_test\server.py' `
    -WorkingDirectory $ServerDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrLogFile `
    -WindowStyle Hidden -PassThru

# --- 4. 轮询健康检查直到版本正确 -------------------------------------------------
Write-Host "[4/4] 等待 $HealthUrl 报告 API_VERSION=$expectedVersion（最多 $TimeoutSeconds 秒）..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$version = $null
$lastError = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    try {
        $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2 -ErrorAction Stop
        if ($resp.api_version) { $version = [string]$resp.api_version; break }
        $lastError = '健康检查已响应，但没有 api_version 字段（说明仍是旧进程）'
    } catch {
        $lastError = $_.Exception.Message
    }
}

if (-not $version) {
    Write-Host ("启动失败：{0} 秒内未就绪。最后错误：{1}" -f $TimeoutSeconds, $lastError) -ForegroundColor Red
    Write-Host ("请查看日志：{0}" -f $ErrLogFile) -ForegroundColor Red
    exit 1
}

$listener = Get-PortOwner $Port
Write-Host ""
if ($expectedVersion -and $version -ne $expectedVersion) {
    Write-Host ("版本不一致：运行中的后端报告 api_version={0}，源码是 {1}；很可能仍是旧进程。" -f $version, $expectedVersion) -ForegroundColor Red
    Write-Host ("（本次启动的 PID {0}，当前占用端口的 PID {1}）" -f $proc.Id, $listener) -ForegroundColor Red
    exit 1
}

Write-Host "服务已重启并运行新代码。" -ForegroundColor Green
Write-Host ("  API_VERSION : {0}" -f $version)
Write-Host ("  启动进程 PID : {0}" -f $proc.Id)
Write-Host ("  监听进程 PID : {0}" -f $listener)
Write-Host ("  地址         : {0}" -f $BaseUrl)
Write-Host ("  日志         : {0}" -f $LogFile)
exit 0
