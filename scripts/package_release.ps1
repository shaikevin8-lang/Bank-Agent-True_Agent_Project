<#
.SYNOPSIS
    阶段 9 提交打包脚本：把「当前提交的可交付内容」打成单个 zip。

.DESCRIPTION
    用途：
        比赛提交需要「仓库 + 视频 + PPT + 文档 PDF」的一致快照。本脚本以
        `git archive` 为准导出**已被 Git 跟踪**的文件（天然不含 .venv、*.db、
        node_modules、.env 等本地产物），再把 docs/deliverables 下已产出的
        演示视频 / PPT / 文档 PDF 一并装入，最后写一份 MANIFEST.txt 记录
        提交哈希、时间、文件数与缺失项（阶段 7 交付物本轮按决定跳过）。

    用法：
        pwsh scripts/package_release.ps1                 # 打到 dist/
        pwsh scripts/package_release.ps1 -OutputDir D:\x # 自定义输出目录
        pwsh scripts/package_release.ps1 -AllowDirty     # 工作区有未提交改动时仍打包

    退出码：
        0 = 打包成功；1 = 前置条件不满足（非 Git 工作树 / 工作区脏且未加 -AllowDirty）

    只读承诺：
        不修改工作树、不改索引；只读 git 命令 + 在输出目录写 zip 与清单。
#>

[CmdletBinding()]
param(
    [string]$OutputDir = '',
    [string]$Tag = '',
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'

if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Get-RepoRoot {
    $root = & git -C $PSScriptRoot rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -ne 0) { throw "无法定位仓库根目录：$PSScriptRoot 不在 Git 工作树内" }
    return ([string]$root).Trim()
}

$repoRoot = Get-RepoRoot
Push-Location $repoRoot
try {
    $dirty = @(& git status --porcelain)
    if ($dirty.Count -gt 0 -and -not $AllowDirty) {
        Write-Host ('[FAIL] 工作区有 ' + $dirty.Count + ' 项未提交改动，提交包必须与某个提交一致。请先提交，或加 -AllowDirty 强制打包。') -ForegroundColor Red
        exit 1
    }

    $hash = ([string](& git rev-parse --short HEAD)).Trim()
    $branch = ([string](& git rev-parse --abbrev-ref HEAD)).Trim()
    if ([string]::IsNullOrWhiteSpace($Tag)) { $Tag = 'phase9-' + $hash }

    if ([string]::IsNullOrWhiteSpace($OutputDir)) { $OutputDir = Join-Path $repoRoot 'dist' }
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
    if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null }

    $stage = Join-Path $OutputDir ('.stage-' + $Tag)
    if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    $repoDir = Join-Path $stage 'repo'
    New-Item -ItemType Directory -Path $repoDir -Force | Out-Null

    Write-Host ('=== package_release: ' + $branch + '@' + $hash + ' -> ' + $OutputDir + ' ===')
    Write-Host '1/4 导出已跟踪文件（git archive HEAD）...'
    $archive = Join-Path $stage 'repo.zip'
    & git archive --format=zip -o $archive HEAD
    if ($LASTEXITCODE -ne 0) { throw 'git archive 失败' }
    Expand-Archive -LiteralPath $archive -DestinationPath $repoDir -Force
    $repoFiles = @(Get-ChildItem -LiteralPath $repoDir -Recurse -File)

    Write-Host '2/4 收集 deliverables（视频 / PPT / PDF，缺失记 MISSING）...'
    $wanted = @('*.mp4', '*.pptx', '*.pdf')
    $present = @()
    $missing = @()
    foreach ($pattern in $wanted) {
        $hits = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'docs/deliverables') -Filter $pattern -Recurse -File -ErrorAction SilentlyContinue)
        if ($hits.Count -gt 0) {
            foreach ($hit in $hits) { $present += $hit.FullName }
        }
        else { $missing += $pattern }
    }
    $artifactDir = Join-Path $stage 'deliverables'
    New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null
    foreach ($file in $present) {
        Copy-Item -LiteralPath $file -Destination (Join-Path $artifactDir (Split-Path $file -Leaf)) -Force
    }

    Write-Host '3/4 写 MANIFEST.txt ...'
    $lines = @()
    $lines += 'Bank-Agent-legacy 提交包清单'
    $lines += ('生成时间     : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'))
    $lines += ('分支@提交     : ' + $branch + '@' + $hash + ('（dirty=' + ($dirty.Count -gt 0) + '）'))
    $lines += ('仓库文件数    : ' + $repoFiles.Count)
    $lines += ('仓库目录大小  : ' + [math]::Round((($repoFiles | Measure-Object -Property Length -Sum).Sum / 1MB), 2) + ' MB')
    $lines += ('deliverables  : ' + (($present | ForEach-Object { Split-Path $_ -Leaf }) -join ', '))
    $lines += ('缺失（按计划跳过）: ' + ($missing -join ', '))
    $lines += ''
    $lines += '说明：repo/ 为 git archive HEAD 解包结果，不含 .venv / *.db / node_modules / .env；'
    $lines += '      docs/deliverables 下阶段 7 的答辩 PPT 与演示视频本轮按决定跳过，故缺失项不写入。'
    $lines | Set-Content -LiteralPath (Join-Path $stage 'MANIFEST.txt') -Encoding UTF8

    Write-Host '4/4 压缩为提交包 ...'
    $outZip = Join-Path $OutputDir ('bank-agent-legacy-' + $Tag + '.zip')
    if (Test-Path $outZip) { Remove-Item -LiteralPath $outZip -Force }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $outZip -CompressionLevel Optimal
    Remove-Item -LiteralPath $stage -Recurse -Force

    $size = [math]::Round(((Get-Item -LiteralPath $outZip).Length / 1MB), 2)
    $sha = (Get-FileHash -LiteralPath $outZip -Algorithm SHA256).Hash
    Write-Host ('[OK] ' + $outZip + '（' + $size + ' MB，sha256=' + $sha.Substring(0, 16) + '…）') -ForegroundColor Green
    Write-Host ('      仓库文件 ' + $repoFiles.Count + ' 个；deliverables ' + $present.Count + ' 个；缺失 ' + $missing.Count + ' 类')
}
finally {
    Pop-Location
}
exit 0
