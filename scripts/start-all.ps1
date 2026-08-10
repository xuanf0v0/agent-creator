param(
    [string]$HarnessRoot = "D:\Projects\my-harness",
    [string]$HarnessUrl = "http://127.0.0.1:8765",
    [switch]$ForceInstall
)

$ErrorActionPreference = "Stop"
$VersionScript = Join-Path $PSScriptRoot "harness-version.ps1"
. $VersionScript

function Get-InstalledHarnessRef([string]$Root) {
    $marker = Join-Path $Root ".installed-ref"
    if (Test-Path -LiteralPath $marker) {
        return (Get-Content -LiteralPath $marker -Raw).Trim()
    }

    $sitePackages = Join-Path $Root ".venv\Lib\site-packages"
    if (-not (Test-Path -LiteralPath $sitePackages)) { return "" }
    $directUrl = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "agent_harness-*.dist-info" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "direct_url.json" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $directUrl) { return "" }
    try {
        $url = (Get-Content -LiteralPath $directUrl -Raw | ConvertFrom-Json).url
        if ($url -match "/archive/([0-9a-fA-F]{40})\.zip$") { return $Matches[1].ToLowerInvariant() }
    } catch {
        return ""
    }
    return ""
}

function Stop-ExpectedHarness([Uri]$Uri, [string]$Root) {
    if ($Uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
        throw "HarnessUrl 不是本机地址，不能自动停止远程 Harness：$Uri"
    }

    $listeners = @(Get-NetTCPConnection -LocalPort $Uri.Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners) { return }

    $expectedExecutable = Join-Path $Root ".venv\Scripts\agent-harness.exe"
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $commandLine = [string]$process.CommandLine
        if (-not $process -or $commandLine.IndexOf($expectedExecutable, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "端口 $($Uri.Port) 被未知进程占用（PID=$($listener.OwningProcess)）；拒绝为升级自动结束"
        }
    }

    foreach ($processId in ($listeners.OwningProcess | Sort-Object -Unique)) {
        Stop-Process -Id $processId -Force
        Write-Host "已停止旧 Harness 监听进程：PID=$processId"
    }

    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort $Uri.Port -State Listen -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 100
    }
    throw "停止旧 Harness 后，端口 $($Uri.Port) 未在 5 秒内释放"
}

$installedRef = Get-InstalledHarnessRef $HarnessRoot
$needsInstall = $ForceInstall -or $installedRef -ne $HarnessPinnedRef
if ($needsInstall) {
    $displayRef = if ($installedRef) { $installedRef } else { "未安装或版本未知" }
    Write-Host "Harness 需要安装/升级：$displayRef -> $HarnessPinnedRef"
    Stop-ExpectedHarness ([Uri]$HarnessUrl) $HarnessRoot
    & (Join-Path $PSScriptRoot "install-harness.ps1") -HarnessRoot $HarnessRoot
    if ($LASTEXITCODE -ne 0) { throw "Harness 安装/升级失败（exit=$LASTEXITCODE）" }
} else {
    Write-Host "Harness 已是预期版本：$HarnessPinnedRef"
}

Write-Host "正在启动全部服务：Harness(8765) + Studio/前端(8787)"
& (Join-Path $PSScriptRoot "start-studio.ps1") -HarnessRoot $HarnessRoot -HarnessUrl $HarnessUrl
if ($LASTEXITCODE -ne 0) { throw "全部服务启动失败（exit=$LASTEXITCODE）" }
