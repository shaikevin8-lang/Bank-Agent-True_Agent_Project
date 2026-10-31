<#
.SYNOPSIS
    阶段 2 受限代码沙箱：容器侧三层隔离实证 + 用例回归校验。

.DESCRIPTION
    只读校验脚本。构建并启动 code-sandbox 服务，轮询 GET /selfcheck 就绪，随后逐项断言三段
    隔离能力（容器网络、凭据零暴露、无宿主挂载、根文件系统只读、/tmp 带 noexec），再跑两组
    不依赖 Docker 的 pytest 用例（逃逸红队 + 适配器契约）；未指定 -KeepUp 时在结束前回收容器。

    唯一 stdout 输出为一段 JSON；所有过程信息一律写 stderr（[Console]::Error.WriteLine），
    因此 stdout 可被调用方直接 ConvertFrom-Json 消费。

.PARAMETER RepoRoot
    仓库根目录。缺省为脚本所在目录的上一级（scripts\..），不在脚本内硬编码绝对路径。

.PARAMETER KeepUp
    保留已启动的容器，便于事后人工排查。缺省 false，即脚本结束前执行 docker compose down。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_code_sandbox.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_code_sandbox.ps1 -KeepUp

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_code_sandbox.ps1 -RepoRoot <仓库根>

.OUTPUTS
    stdout 仅一段 JSON：
    {"passed":<int>,"failed":<int>,"skipped":[<str>...],"checks":[{"name":..,"ok":bool,"detail":..}],"docker":true}

.EXITCODE
    0 = failed 为 0，或 docker 不可用（此时 skipped 显式标注 docker-unavailable，属「未验证」而非「通过」）；
    1 = 存在失败项，checks 中 ok=false 的条目即失败原因。

.NOTES
    兼容 PowerShell 5.1 及以上；输出统一 UTF-8。
    只读承诺：脚本不修改仓库任何文件；容器侧仅在只读根与 tmpfs /tmp 上做写入探测，探测失败即期望结果。
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = '',
    [switch] $KeepUp
)

$ErrorActionPreference = 'Stop'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$ProgressPreference = 'SilentlyContinue'

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
try {
    [Console]::OutputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
} catch { }

$script:Checks      = New-Object System.Collections.ArrayList
$script:Skipped     = New-Object System.Collections.ArrayList
$script:ComposeSvc  = 'code-sandbox'
$script:ContainerNm = 'bank-agent-code-sandbox'

function Write-Log {
    param([string] $Message)
    try { [Console]::Error.WriteLine('[verify_code_sandbox] ' + $Message) } catch { }
}

function Add-Check {
    param([string] $Name, [bool] $Ok, [string] $Detail)
    $d = ''
    if ($null -ne $Detail) { $d = ($Detail -replace '\s+', ' ').Trim() }
    if ($d.Length -gt 400) { $d = $d.Substring(0, 400) + ' ...' }
    [void]$script:Checks.Add([pscustomobject]@{ name = $Name; ok = $Ok; detail = $d })
    $flag = 'FAIL'
    if ($Ok) { $flag = 'ok  ' }
    Write-Log ('[{0}] {1} :: {2}' -f $flag, $Name, $d)
}

function Add-Skip {
    param([string] $Reason)
    if (-not $script:Skipped.Contains($Reason)) { [void]$script:Skipped.Add($Reason) }
}

function Write-Result {
    param([bool] $DockerAvailable)
    $passed = 0
    $failed = 0
    foreach ($c in $script:Checks) { if ($c.ok) { $passed++ } else { $failed++ } }
    $skipped = @()
    foreach ($s in $script:Skipped) { $skipped += [string]$s }
    $checks = @()
    foreach ($c in $script:Checks) { $checks += $c }
    $obj = [pscustomobject]@{
        passed  = [int]$passed
        failed  = [int]$failed
        skipped = $skipped
        checks  = $checks
        docker  = $DockerAvailable
    }
    $json = $obj | ConvertTo-Json -Depth 6 -Compress
    try { [Console]::Out.WriteLine($json) } catch { Write-Output $json }
}

# 原生命令统一入口：PowerShell 5.1 在 $ErrorActionPreference='Stop' 下，把原生 stderr 经 2>&1
# 合并后会生成 ErrorRecord 并抛 NativeCommandError（docker build 的进度输出就会触发），
# 因此这里局部把 ErrorActionPreference 降为 Continue，只取退出码与合并后的文本。
function Invoke-Native {
    param([string] $FilePath, [string[]] $ArgList = @())
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    $text = ''
    try {
        $records = & $FilePath @ArgList 2>&1
        $code = $LASTEXITCODE
        $lines = @()
        foreach ($rec in $records) {
            if ($rec -is [System.Management.Automation.ErrorRecord]) { $lines += [string]$rec.Exception.Message }
            else { $lines += [string]$rec }
        }
        $text = ($lines -join [Environment]::NewLine)
    } catch {
        $text = $_.Exception.Message
        $code = 1
    } finally {
        $ErrorActionPreference = $saved
    }
    return [pscustomobject]@{ exit = [int]$code; output = [string]$text }
}

function Test-DockerAvailable {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { return $false }
    $r = Invoke-Native -FilePath 'docker' -ArgList @('version', '--format', '{{.Server.Version}}')
    if ($r.exit -ne 0) { return $false }
    if ([string]::IsNullOrWhiteSpace($r.output)) { return $false }
    Write-Log ('docker server version: ' + $r.output.Trim())
    return $true
}

function Invoke-Compose {
    param([string[]] $ComposeArgs)
    return Invoke-Native -FilePath 'docker' -ArgList (@('compose') + $ComposeArgs)
}

# 注意：PowerShell 5.1 把参数交给原生程序时会吞掉参数内嵌的双引号，
# 因此 -c 传入的内嵌 Python 片段一律只用单引号字面量，避免 SyntaxError。
function Invoke-InContainer {
    param([string[]] $CommandArgs)
    $r = Invoke-Compose (@('exec', '-T', $script:ComposeSvc) + $CommandArgs)
    if ($r.exit -eq 0) { return $r }
    return Invoke-Native -FilePath 'docker' -ArgList (@('exec', $script:ContainerNm) + $CommandArgs)
}

function Get-ComposeFile {
    param([string] $Root)
    foreach ($name in @('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')) {
        if (Test-Path -LiteralPath (Join-Path $Root $name)) { return $name }
    }
    return $null
}

function Invoke-Verification {
    param([string] $Root, [bool] $Keep)

    $composeFile = Get-ComposeFile -Root $Root
    if ($null -eq $composeFile) {
        Add-Check 'compose-file' $false ('未找到根编排文件（docker-compose.yml）：仓库根 ' + $Root + ' 下 docker/code-sandbox 与根 compose 由并行基础设施子任务落地，当前缺失，容器侧断言无法执行')
        Add-Skip 'compose-file-missing'
        return
    }
    Add-Check 'compose-file' $true ('编排文件：' + $composeFile)

    Write-Log ('docker compose up -d --build ' + $script:ComposeSvc)
    $up = Invoke-Compose @('up', '-d', '--build', $script:ComposeSvc)
    Add-Check 'compose-up' ($up.exit -eq 0) ('exit=' + $up.exit + '; ' + $up.output.Trim())
    if ($up.exit -ne 0) {
        Add-Skip 'compose-up-failed'
        return
    }

    $pySelfcheck = @'
import json, urllib.request
data = urllib.request.urlopen('http://127.0.0.1:8022/selfcheck', timeout=3).read().decode('utf-8')
print(json.dumps(json.loads(data), ensure_ascii=True))
'@
    $deadline = (Get-Date).AddSeconds(60)
    $selfcheck = $null
    $lastErr = ''
    $reUpped = $false
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-InContainer @('python', '-c', $pySelfcheck)
        if ($r.exit -eq 0 -and -not [string]::IsNullOrWhiteSpace($r.output)) {
            try {
                $selfcheck = $r.output.Trim() | ConvertFrom-Json
                break
            } catch {
                $lastErr = 'selfcheck JSON 解析失败：' + $_.Exception.Message
            }
        } else {
            $lastErr = ($r.output -replace '\s+', ' ').Trim()
            if (-not $reUpped -and $lastErr -match 'No such container') {
                Write-Log '容器在轮询期间消失（疑似被并发 compose down 回收），重试 docker compose up -d。'
                $null = Invoke-Compose @('up', '-d', $script:ComposeSvc)
                $reUpped = $true
            }
        }
        Start-Sleep -Seconds 2
    }

    $endpointOk = ($null -ne $selfcheck)
    $endpointDetail = '容器内 GET http://127.0.0.1:8022/selfcheck 60s 内未就绪：' + $lastErr
    if ($endpointOk) { $endpointDetail = '容器内 GET /selfcheck 已就绪' }
    Add-Check 'selfcheck-endpoint' $endpointOk $endpointDetail

    if ($endpointOk) {
        Add-Check 'selfcheck-network' ($selfcheck.network -eq $false) ('network = ' + $selfcheck.network)
        Add-Check 'selfcheck-credentials' (([int]$selfcheck.credentials) -eq 0) ('credentials = ' + $selfcheck.credentials)
        $mountList = @($selfcheck.mounts)
        Add-Check 'selfcheck-mounts' ($mountList.Count -eq 0) ('mounts = [' + ($mountList -join ', ') + ']')
    } else {
        Add-Skip 'selfcheck-unavailable'
    }

    $pyEgress = @'
import urllib.request
try:
    urllib.request.urlopen('https://example.com', timeout=5)
    print('REACHED')
except Exception as exc:
    print('BLOCKED ' + type(exc).__name__)
'@
    $eg = Invoke-InContainer @('python', '-c', $pyEgress)
    $egText = ($eg.output -replace '\s+', ' ').Trim()
    Add-Check 'egress-blocked' ($eg.exit -eq 0 -and $egText.StartsWith('BLOCKED')) $egText

    $pyEnv = @'
import json, os
print(json.dumps({'count': len(os.environ), 'keys': sorted(os.environ.keys())}, ensure_ascii=True))
'@
    $envr = Invoke-InContainer @('python', '-c', $pyEnv)
    $allow = @('PATH', 'HOSTNAME', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'GPG_KEY', 'PYTHON_VERSION',
        'PYTHON_SHA256', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'PYTHONIOENCODING',
        'PWD', 'SHLVL', 'TERM', '_', 'SANDBOX_IMAGE')
    $envObj = $null
    if ($envr.exit -eq 0 -and -not [string]::IsNullOrWhiteSpace($envr.output)) {
        try { $envObj = $envr.output.Trim() | ConvertFrom-Json } catch { $envObj = $null }
    }
    if ($null -ne $envObj) {
        $keys = @($envObj.keys)
        $others = @($keys | Where-Object { $allow -notcontains $_ })
        $suspicious = @($others | Where-Object { $_ -match '(?i)(TOKEN|SECRET|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|DSN|MYSQL|REDIS|POSTGRES|OPENAI|DEEPSEEK|API_?KEY)' })
        $envOk = ($suspicious.Count -eq 0 -and $others.Count -le 3)
        $envDetail = 'os.environ 条目=' + $envObj.count + '；非镜像白名单键=[' + ($others -join ', ') + ']；疑似凭据键=[' + ($suspicious -join ', ') + ']'
        Add-Check 'env-near-empty' $envOk $envDetail
    } else {
        Add-Check 'env-near-empty' $false ('容器内枚举 os.environ 失败：' + ($envr.output -replace '\s+', ' ').Trim())
    }

    $ro = Invoke-InContainer @('touch', '/probe')
    Add-Check 'root-readonly' ($ro.exit -ne 0) ('容器内 touch /probe 退出码=' + $ro.exit + '（期望非 0）；' + ($ro.output -replace '\s+', ' ').Trim())

    $pyTmp = @'
rows = []
with open('/proc/mounts', 'r', encoding='utf-8', errors='replace') as fh:
    for line in fh:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == '/tmp':
            rows.append(line.strip())
print(' || '.join(rows) if rows else 'NO_TMP_ENTRY')
'@
    $tmp = Invoke-InContainer @('python', '-c', $pyTmp)
    $tmpText = ($tmp.output -replace '\s+', ' ').Trim()
    Add-Check 'tmp-noexec' ($tmp.exit -eq 0 -and $tmpText -match 'noexec') $tmpText

    $ports = Invoke-Native -FilePath 'docker' -ArgList @('port', $script:ContainerNm)
    $portsText = ($ports.output -replace '\s+', '').Trim()
    Add-Check 'no-published-ports' ([string]::IsNullOrEmpty($portsText)) ('docker port ' + $script:ContainerNm + ' 输出=[' + ($ports.output -replace '\s+', ' ').Trim() + ']')

    $netRaw = Invoke-Native -FilePath 'docker' -ArgList @('inspect', '-f', '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}', $script:ContainerNm)
    $netText = ($netRaw.output -replace '\s+', ' ').Trim()
    $netName = ''
    if (-not [string]::IsNullOrWhiteSpace($netText)) { $netName = ($netText -split ' ')[0] }
    if (-not [string]::IsNullOrWhiteSpace($netName)) {
        $internalRaw = Invoke-Native -FilePath 'docker' -ArgList @('network', 'inspect', $netName, '-f', '{{.Internal}}')
        $internalText = ($internalRaw.output -replace '\s+', '').Trim()
        Add-Check 'sandbox-net-internal' ($internalText -eq 'true') ('network=' + $netName + ' Internal=' + $internalText)
    } else {
        Add-Check 'sandbox-net-internal' $false '无法解析容器所属网络（docker inspect .NetworkSettings.Networks 为空）'
    }

    $pyExe = $null
    $pyPrefix = @()
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) { $pyExe = $venvPy }
    elseif ($null -ne (Get-Command python -ErrorAction SilentlyContinue)) { $pyExe = 'python' }
    elseif ($null -ne (Get-Command py -ErrorAction SilentlyContinue)) { $pyExe = 'py'; $pyPrefix = @('-3') }

    $needed = @('tests\security\test_sandbox_escape.py', 'tests\contract\test_code_compute.py')
    $missing = @()
    foreach ($rel in $needed) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $rel))) { $missing += $rel }
    }
    if ($null -eq $pyExe) {
        Add-Check 'pytest-cases' $false '宿主未找到 python/py，无法运行 pytest'
        Add-Skip 'python-unavailable'
    } elseif ($missing.Count -gt 0) {
        Add-Check 'pytest-cases' $false ('用例文件缺失：' + ($missing -join ', ') + '（并行子任务尚未落地 tests/ 下的沙箱用例）')
        Add-Skip 'test-files-missing'
    } else {
        $pytestArgs = $pyPrefix + @('-m', 'pytest', 'tests/security/test_sandbox_escape.py', 'tests/contract/test_code_compute.py', '-q')
        $pytestRun = Invoke-Native -FilePath $pyExe -ArgList $pytestArgs
        Add-Check 'pytest-cases' ($pytestRun.exit -eq 0) ('exit=' + $pytestRun.exit + '; ' + $pytestRun.output.Trim())
    }

    if (-not $Keep) {
        $dn = Invoke-Compose @('down')
        Add-Check 'compose-down' ($dn.exit -eq 0) ('docker compose down exit=' + $dn.exit + '; ' + $dn.output.Trim())
    } else {
        Write-Log '跳过 docker compose down（-KeepUp）'
    }
}

$root = $RepoRoot
if ([string]::IsNullOrWhiteSpace($root)) { $root = Split-Path -Parent $PSScriptRoot }

$dockerOk = Test-DockerAvailable
if (-not $dockerOk) {
    Add-Skip 'docker-unavailable'
    Write-Log 'docker 不可用：跳过全部容器侧断言，输出 docker=false。'
    Write-Result $false
    exit 0
}

$pushed = $false
try {
    if (-not (Test-Path -LiteralPath $root)) {
        Add-Check 'repo-root' $false ('仓库根不存在：' + $root)
    } else {
        Push-Location -LiteralPath $root
        $pushed = $true
        Add-Check 'repo-root' $true ('仓库根：' + (Get-Location).Path)
        Invoke-Verification -Root $root -Keep ([bool]$KeepUp)
    }
} catch {
    Add-Check 'script-exception' $false ($_.Exception.Message)
} finally {
    if ($pushed) { Pop-Location }
}

$failedCount = 0
foreach ($c in $script:Checks) { if (-not $c.ok) { $failedCount++ } }
Write-Result $true
if ($failedCount -eq 0) { exit 0 } else { exit 1 }
