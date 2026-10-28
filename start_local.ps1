# 本地启动/重启脚本（本机环境专用，未纳入版本库，可删除）
#
# 背景：web_test/server.py 启动时只校验 Alembic 版本、不自动建表，因此必须有可用的
# 数据库连接串；而 restart_server.ps1 用 Start-Process 起服务，会留下可被误关的窗口。
# 本脚本改为：
#   1) 确保 Docker 容器 bank-agent-pg 在跑（独立库 bank_agent_legacy，不动别的项目的 bank_agent 库）；
#   2) 只结束命令行含 web_test\server.py 的 python 进程；
#   3) 用 wscript.exe + start_server.vbs 以「完全隐藏、无窗口」的方式拉起服务；
#   4) 轮询 /health/live 确认就绪。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\start_local.ps1

$ErrorActionPreference = 'Stop'

$Root       = $PSScriptRoot
$ServerDir  = Join-Path $Root 'Struct_of_Agent'
$Vbs        = Join-Path $Root 'start_server.vbs'
$BaseUrl    = 'http://127.0.0.1:8000'
$HealthUrl  = "$BaseUrl/health/live"
$TimeoutSec = 25

Write-Host '=== 启动银行智能 Agent（web_test 本地服务，端口 8000） ===' -ForegroundColor Cyan

# 1) 数据库容器
if (-not (docker ps --filter 'name=^bank-agent-pg$' --format '{{.Names}}')) {
    Write-Host '[1/4] 启动数据库容器 bank-agent-pg ...' -ForegroundColor Yellow
    docker start bank-agent-pg | Out-Null
    Start-Sleep -Seconds 3
} else {
    Write-Host '[1/4] 数据库容器 bank-agent-pg 已在运行。'
}

# 2) 结束旧的本项目进程（只匹配 web_test\server.py，不误杀别的 python）
$old = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'web_test[\\/]server\.py' })
if ($old.Count -gt 0) {
    Write-Host ("[2/4] 结束旧进程：" + (($old | ForEach-Object { $_.ProcessId }) -join ', '))
    foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(6)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 300
    }
} else {
    Write-Host '[2/4] 没有运行中的旧进程。'
}

$portOwner = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portOwner) {
    Write-Host ("端口 8000 仍被 PID {0} 占用，已中止（避免误杀其它程序）。" -f $portOwner.OwningProcess) -ForegroundColor Red
    exit 1
}

# 3) 隐藏启动
Write-Host '[3/4] 隐藏启动服务（wscript + start_server.vbs，无窗口，关掉终端也不停）...'
# 通过 WMI 创建进程：父进程不是当前 shell，因此关掉终端 / 本脚本退出都不会带走服务。
$wmi = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "wscript.exe `"$Vbs`""
}
if ($wmi.ReturnValue -ne 0) {
    Write-Host ("创建进程失败，ReturnValue={0}" -f $wmi.ReturnValue) -ForegroundColor Red
    exit 1
}

# 4) 健康检查
Write-Host "[4/4] 等待 $HealthUrl 就绪（最多 $TimeoutSec 秒）..."
$version = $null
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 600
    try {
        $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($resp.api_version) { $version = [string]$resp.api_version; break }
    } catch { }
}

if (-not $version) {
    Write-Host '启动失败，请查看日志：Struct_of_Agent\web_test\server.err.log' -ForegroundColor Red
    exit 1
}

$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Write-Host ''
Write-Host '服务已就绪。' -ForegroundColor Green
Write-Host ("  API_VERSION : {0}" -f $version)
Write-Host ("  监听 PID    : {0}" -f $listener.OwningProcess)
Write-Host ("  地址        : {0}" -f $BaseUrl)
Write-Host ("  日志        : {0}" -f (Join-Path $ServerDir 'web_test\server.log'))
exit 0
