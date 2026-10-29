<#
.SYNOPSIS
    提交前仓库卫生检查（只读，不修改仓库状态）。

.DESCRIPTION
    用途：
        在 git commit 之前扫描 Git 索引（git ls-files / git grep），拦截不该入库的内容——
        缓存与编译产物、本地数据库与日志、真实环境变量文件、历史残迹目录（apps/、packages/），
        以及硬编码的本机绝对路径与明文口令字面量。用于「阶段 0 T0.4 防回归」，防止已清理过的
        脏东西重新回到版本库。

    用法：
        pwsh scripts/check_repo_hygiene.ps1

    退出码：
        0 = PASS  全部检查通过（第 6 项工作树提示不影响结论）
        1 = FAIL  至少一项检查在索引中命中违规条目

    检查项：
        1. 索引中不得有 __pycache__/、*.pyc、*.db、*.log、.pytest_cache/、.venv/、node_modules/
        2. 索引中不得有 .env / *.env 等真实环境文件（允许模板 .env.example）
        3. 索引中不得有 apps/、packages/ 目录（D14 防回归）
        4. 索引中不得出现硬编码本机绝对项目路径
        5. *.vbs 与 *.ps1 中不得出现口令字面量 bank_local
        6. 工作树状态提示（仅提示，不计 FAIL）

    对应开发文档：阶段 0 —— T0.4 防回归（提交前卫生检查脚本）。

    兼容性：
        Windows PowerShell 5.1 与 PowerShell 7 均可运行（未使用 ??、-Parallel 等 PS7 专有语法）。
        脚本自身路径由 $PSScriptRoot 推导仓库根，不含任何硬编码绝对路径。

    只读承诺：
        只调用 git 的只读子命令（rev-parse / ls-files / grep / status），不写索引、不改工作树。
#>

$ErrorActionPreference = 'Stop'

# PowerShell 7.3+ 若启用 $PSNativeCommandUseErrorActionPreference，git grep 无命中时的退出码 1
# 会被当成终止性错误抛出；这里显式关闭。Windows PowerShell 5.1 没有该变量，跳过即可。
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$MaxShownLines = 20

function Get-RepoRoot {
    # 由脚本自身位置推仓库根：禁止硬编码绝对路径。
    $root = & git -C $PSScriptRoot rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "无法定位仓库根目录：$PSScriptRoot 不在 Git 工作树内"
    }
    $root = ([string]$root).Trim()
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw '无法定位仓库根目录：git rev-parse 返回空'
    }
    return $root
}

function Invoke-GitRead {
    # 只读 git 调用；退出码 0 = 有输出，1 = 无命中（对 grep 而言属正常），>1 = 真实错误。
    param([string[]]$GitArgs)

    $output = & git -C $repoRoot -c core.quotepath=false @GitArgs 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -gt 1) {
        throw ("git 只读命令失败（exit=$exitCode）：git " + ($GitArgs -join ' '))
    }
    if ($null -eq $output) {
        return @()
    }
    return @($output)
}

function Add-CheckResult {
    param(
        [string]$Name,
        [string[]]$Hits
    )

    $lines = @($Hits | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })

    if ($lines.Count -eq 0) {
        Write-Host ("[OK] " + $Name) -ForegroundColor Green
        return
    }

    $script:FailCount = $script:FailCount + 1
    Write-Host ("[FAIL] " + $Name) -ForegroundColor Red

    if ($lines.Count -gt $MaxShownLines) {
        $shown = $lines[0..($MaxShownLines - 1)]
    }
    else {
        $shown = $lines
    }
    foreach ($line in $shown) {
        Write-Host ("       " + $line) -ForegroundColor DarkGray
    }
    if ($lines.Count -gt $MaxShownLines) {
        Write-Host ("       ... 另有 " + ($lines.Count - $MaxShownLines) + " 行未显示") -ForegroundColor DarkYellow
    }
}

$script:FailCount = 0
$repoRoot = Get-RepoRoot

Push-Location $repoRoot
try {
    $indexFiles = @(Invoke-GitRead -GitArgs @('ls-files'))

    Write-Host '=== check_repo_hygiene: 提交前仓库卫生检查（只读） ==='
    Write-Host ("仓库根 : " + $repoRoot)
    Write-Host ("索引条目: " + $indexFiles.Count)
    Write-Host ''

    # ---- 检查 1：缓存 / 编译产物 / 本地数据库 / 日志 ----
    $junkPatterns = @(
        '(^|/)__pycache__/',
        '\.pyc$',
        '\.db$',
        '\.log$',
        '(^|/)\.pytest_cache/',
        '(^|/)\.venv/',
        '(^|/)node_modules/'
    )
    $junkHits = @()
    foreach ($path in $indexFiles) {
        foreach ($pattern in $junkPatterns) {
            if ($path -match $pattern) {
                $junkHits += ($path + '  <- ' + $pattern)
            }
        }
    }
    Add-CheckResult -Name '索引中无缓存/编译产物/本地数据库/日志（__pycache__、*.pyc、*.db、*.log、.pytest_cache、.venv、node_modules）' -Hits $junkHits

    # ---- 检查 2：真实环境变量文件（允许模板 .env.example） ----
    $envHits = @()
    foreach ($path in $indexFiles) {
        $name = ($path -split '/')[-1]
        if ($name -eq '.env.example') {
            continue
        }
        if ($name -eq '.env' -or $name -like '.env.*' -or $name -like '*.env') {
            $envHits += ($path + '  <- 真实环境变量文件不得入库，模板请命名为 .env.example')
        }
    }
    Add-CheckResult -Name '索引中无真实环境变量文件（.env / *.env；允许 .env.example）' -Hits $envHits

    # ---- 检查 3：历史残迹目录 apps/、packages/ ----
    $legacyHits = @()
    foreach ($path in $indexFiles) {
        if ($path -match '^(apps|packages)/') {
            $legacyHits += ($path + '  <- 历史残迹目录（D14 防回归）')
        }
    }
    Add-CheckResult -Name '索引中无历史残迹目录 apps/、packages/（D14 防回归）' -Hits $legacyHits

    # ---- 检查 4：硬编码本机绝对项目路径 ----
    # 说明：检索串用片段拼接而非完整字面量，避免本脚本自身被本项检查命中（自证陷阱）。
    $pathSeparator = [string][char]0x5C
    $hardcodedNeedle = 'D:' + $pathSeparator + 'Bank-Agent' + '-legacy'
    $hardcodedHits = @(Invoke-GitRead -GitArgs @('grep', '-n', '-I', '-F', '-e', $hardcodedNeedle))
    Add-CheckResult -Name '索引文件中无硬编码本机绝对项目路径' -Hits $hardcodedHits

    # ---- 检查 5：脚本中的明文口令字面量 ----
    $secretHits = @(Invoke-GitRead -GitArgs @('grep', '-n', '-I', '-F', '-e', 'bank_local', '--', '*.vbs', '*.ps1'))
    Add-CheckResult -Name '*.vbs/*.ps1 中无明文口令字面量 bank_local' -Hits $secretHits

    # ---- 检查 6：工作树状态提示（仅提示，不计 FAIL） ----
    $tipPatterns = @(
        '__pycache__',
        '\.pyc$',
        '\.db$',
        '\.db-journal$',
        '\.sqlite3?$',
        '\.log$',
        '\.pytest_cache',
        '\.pytest-tmp',
        '\.venv/',
        'node_modules/',
        '\.env$',
        '\.env\.[A-Za-z]+$'
    )
    $statusLines = @(Invoke-GitRead -GitArgs @('status', '--porcelain'))
    $tips = @()
    foreach ($line in $statusLines) {
        if ($line -match '\.env\.example') {
            continue
        }
        foreach ($pattern in $tipPatterns) {
            if ($line -match $pattern) {
                $tips += $line
                break
            }
        }
    }
    if ($tips.Count -eq 0) {
        Write-Host '[OK] 工作树无本地垃圾文件（提示项，不计入 FAIL）' -ForegroundColor Green
    }
    else {
        Write-Host '[TIP] 工作树存在本地文件/目录（仅提示，不计入 FAIL；提交时勿用 git add -A / git add .）' -ForegroundColor Yellow
        $shownTips = $tips
        if ($tips.Count -gt $MaxShownLines) {
            $shownTips = $tips[0..($MaxShownLines - 1)]
        }
        foreach ($line in $shownTips) {
            Write-Host ('       ' + $line) -ForegroundColor DarkGray
        }
        if ($tips.Count -gt $MaxShownLines) {
            Write-Host ('       ... 另有 ' + ($tips.Count - $MaxShownLines) + ' 行未显示') -ForegroundColor DarkYellow
        }
    }

    Write-Host ''
    if ($FailCount -eq 0) {
        Write-Host 'check_repo_hygiene: PASS' -ForegroundColor Green
        exit 0
    }
    Write-Host ('check_repo_hygiene: FAIL（' + $FailCount + ' 项检查未通过）') -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
