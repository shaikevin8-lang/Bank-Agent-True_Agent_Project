<#
.SYNOPSIS
    扫描仓库内所有 Markdown 中出现的错误码 token，与基座冻结清单比对。

.DESCRIPTION
    基座冻结清单（docs/AI_Banking_Agent_缺失内容补齐开发文档.md 第 106 行，未经 ADR 不得增删）：
      AUTH_001 AUTHZ_001 VALIDATION_001 CONFLICT_001
      NOT_READY_001 RATE_LIMIT_001 UNKNOWN_RESULT_001 INTERNAL_001

    token 形态收紧为「全大写单词 + 数字后缀恰好 3 位」，即 XXX_001 / XXX_YYY_001：
      - 数字后缀必须用下划线连接，因此 FIG-01、ADR-005、TC-XXX-01、FR-COM-003 不会被命中；
      - 要求全大写单词，因此 RLIMIT_CPU、circuit_breaker_state、R1 不会被命中；
      - 后缀严格 3 位数字，因此 AUTH_0001 这类更宽写法不会被命中。

    输出（stdout 只有这一行 JSON，便于管道消费）：
      {"passed":<bool>,"scanned_files":<int>,"codes_found":[...],"unknown":[{"code","file","line"}]}
    存在清单外 token 时退出码 1，否则退出码 0；诊断信息只写 stderr。

.PARAMETER Root
    仓库根目录，默认取本脚本所在目录的上级（即 scripts/ 的父目录）。

.EXAMPLE
    pwsh scripts/check_error_codes.ps1
    $r = pwsh scripts/check_error_codes.ps1 | ConvertFrom-Json; $r.passed
#>
[CmdletBinding()]
param(
    [string]$Root
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = Split-Path -Parent $PSScriptRoot
}
$Root = (Resolve-Path -LiteralPath $Root).Path

# 基座冻结清单：唯一允许出现在文档里的错误码集合。
$Frozen = @(
    'AUTH_001',
    'AUTHZ_001',
    'VALIDATION_001',
    'CONFLICT_001',
    'NOT_READY_001',
    'RATE_LIMIT_001',
    'UNKNOWN_RESULT_001',
    'INTERNAL_001'
)

# 历史文档中出现过、但从未进入冻结清单的 token 的显式豁免：只豁免扫描，不放宽基座。
# 出处：docs/AI_Banking_Agent_缺失内容补齐开发文档.md 第 17 行「E2 错误码」，
#       原文明确该码被否决（冻结清单无此码 → 熔断拦截复用 NOT_READY_001），属历史讨论文本。
$FrozenExtra = @(
    'CIRCUIT_OPEN_001'
)

$AllowList = @($Frozen + $FrozenExtra)

# 全大写单词 + 下划线分组 + 恰好 3 位数字后缀；前后不得紧贴字母/数字/下划线。
$CodePattern = '(?<![A-Za-z0-9_])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_\d{3})(?![A-Za-z0-9_])'

# 扫描时排除的目录（虚拟环境、依赖、版本库元数据、pytest 临时目录）。
$ExcludeSegments = @('.venv', 'node_modules', '.git', '.pytest-tmp')

function Get-RelativePath {
    param([string]$Base, [string]$Full)
    $baseCount = @($Base -split '[\\/]' | Where-Object { $_ -ne '' }).Count
    $parts = @($Full -split '[\\/]' | Where-Object { $_ -ne '' })
    if ($parts.Count -le $baseCount) { return ($Full -replace '\\', '/') }
    return (@($parts[$baseCount..($parts.Count - 1)]) -join '/')
}

$files = @(
    Get-ChildItem -LiteralPath $Root -Recurse -File -Filter '*.md' | Where-Object {
        $relative = Get-RelativePath -Base $Root -Full $_.FullName
        $segments = $relative -split '/'
        $excluded = $false
        foreach ($segment in $segments) {
            if ($ExcludeSegments -contains $segment) { $excluded = $true; break }
        }
        -not $excluded
    } | Sort-Object FullName
)

$codesFound = New-Object 'System.Collections.Generic.SortedSet[string]'
$unknown = New-Object 'System.Collections.ArrayList'

foreach ($file in $files) {
    $relativePath = Get-RelativePath -Base $Root -Full $file.FullName
    $lineNo = 0
    foreach ($line in [System.IO.File]::ReadAllLines($file.FullName, [System.Text.Encoding]::UTF8)) {
        $lineNo++
        foreach ($match in [regex]::Matches($line, $CodePattern)) {
            $code = $match.Groups[1].Value
            [void]$codesFound.Add($code)
            if ($AllowList -notcontains $code) {
                [void]$unknown.Add([pscustomobject]@{
                        code = $code
                        file = $relativePath
                        line = $lineNo
                    })
            }
        }
    }
}

$passed = ($unknown.Count -eq 0)

$report = [pscustomobject]@{
    passed        = $passed
    scanned_files = $files.Count
    codes_found   = @($codesFound)
    unknown       = @($unknown)
}

# stdout 只保留报告 JSON（单行），其它诊断一律走 stderr。
$json = $report | ConvertTo-Json -Depth 6 -Compress
[Console]::Out.Write($json)
[Console]::Out.Write("`n")

if (-not $passed) {
    [Console]::Error.WriteLine("check_error_codes: 发现 $($unknown.Count) 处清单外错误码，详见 unknown 字段。")
    exit 1
}

[Console]::Error.WriteLine("check_error_codes: OK（扫描 $($files.Count) 个 Markdown，未发现清单外错误码）。")
exit 0
