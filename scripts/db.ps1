<#
.SYNOPSIS
    Bank-Agent 数据库运维入口（Windows）。

.DESCRIPTION
    统一包装 scripts/db.py，并在执行前显示目标库（隐藏口令）。
    reset 子命令在未显式传 --confirm 时会要求输入确认令牌。

.EXAMPLE
    .\scripts\db.ps1 upgrade
    .\scripts\db.ps1 check
    .\scripts\db.ps1 seed
    .\scripts\db.ps1 import-sqlite --data-dir Struct_of_Agent\web_test\data
    .\scripts\db.ps1 reset --env demo
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

if (-not $env:BANK_AGENT_DB_URL -and -not $env:DATABASE_URL) {
    Write-Error "未配置数据库连接：请先设置 `$env:BANK_AGENT_DB_URL（例如 postgresql+psycopg://user:pass@localhost:5432/bank_agent）"
    exit 2
}

$rawUrl = if ($env:BANK_AGENT_DB_URL) { $env:BANK_AGENT_DB_URL } else { $env:DATABASE_URL }
$maskedUrl = [regex]::Replace($rawUrl, '://([^:/@]+):([^@]*)@', '://$1:***@')
Write-Host "目标库：$maskedUrl" -ForegroundColor Cyan
if ($env:BANK_AGENT_ENV) {
    Write-Host "环境：$env:BANK_AGENT_ENV" -ForegroundColor Cyan
}

# reset 的二次意图确认：确认令牌不是业务金融确认，只用于防误操作。
if ($Arguments.Count -gt 0 -and $Arguments[0] -eq 'reset' -and ($Arguments -notcontains '--confirm')) {
    $answer = Read-Host "将清空 app schema 的演示数据（audit 保留）。请输入 RESET-DEMO-DATA 继续"
    if ($answer -ne 'RESET-DEMO-DATA') {
        Write-Host '已取消。' -ForegroundColor Yellow
        exit 2
    }
    $Arguments += @('--confirm', 'RESET-DEMO-DATA')
}

& $python (Join-Path $PSScriptRoot 'db.py') @Arguments
exit $LASTEXITCODE
