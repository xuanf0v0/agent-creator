param(
    [string]$HarnessRoot = "D:\Projects\my-harness",
    [string]$HarnessUrl = "http://127.0.0.1:8765",
    [switch]$SkipHarness
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeEnv = Join-Path $HarnessRoot ".runtime.env"
$TaskTokenLine = Get-Content -LiteralPath $RuntimeEnv | Where-Object { $_.StartsWith("AGENT_HARNESS_TASK_TOKEN=") } | Select-Object -First 1
if (-not $TaskTokenLine) { throw "缺少 Harness 任务 Token" }
Add-Type -AssemblyName System.Net.Http

function Test-HarnessReady([string]$Url) {
    $handler = $null
    $client = $null
    try {
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.UseProxy = $false
        $client = New-Object System.Net.Http.HttpClient -ArgumentList $handler
        $client.Timeout = [TimeSpan]::FromSeconds(2)
        $response = $client.GetAsync(($Url.TrimEnd("/") + "/api/v1/capabilities")).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { return $false }
        $payload = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        return ([string]$payload.api.selected_version -eq "1")
    } catch {
        return $false
    } finally {
        if ($client) { $client.Dispose() }
        if ($handler) { $handler.Dispose() }
    }
}

function Assert-HarnessInstance([string]$Url, [string]$Root) {
    $instanceFile = Join-Path (Join-Path $Root "state") "instance.json"
    if (-not (Test-Path -LiteralPath $instanceFile)) { throw "8765 已有 Harness，但缺少预期 state instance；拒绝接入未知实例" }
    $expected = (Get-Content -LiteralPath $instanceFile -Raw | ConvertFrom-Json).instance_id
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient -ArgumentList $handler
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(2)
        $actual = ($client.GetStringAsync(($Url.TrimEnd("/") + "/api/v1/capabilities")).GetAwaiter().GetResult() | ConvertFrom-Json).instance.instance_id
    } finally { $client.Dispose(); $handler.Dispose() }
    if (-not $expected -or $expected -ne $actual) { throw "8765 上的 Harness instance 与预期 state 不匹配；拒绝继续" }
}

$harnessUri = [Uri]$HarnessUrl
if (-not $SkipHarness -and $harnessUri.Host -in @("127.0.0.1", "localhost", "::1")) {
    $harnessScript = Join-Path $PSScriptRoot "start-harness.ps1"
    if (-not (Test-HarnessReady $HarnessUrl)) {
        $hostAddress = if ($harnessUri.Host -eq "localhost") { "127.0.0.1" } else { $harnessUri.Host }
        $logRoot = Join-Path $ProjectRoot ".harness"
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $harnessStdout = Join-Path $logRoot "start-harness.stdout.log"
        $harnessStderr = Join-Path $logRoot "start-harness.stderr.log"
        $harnessArgs = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $harnessScript),
            "-HarnessRoot", ('"{0}"' -f $HarnessRoot), "-HostAddress", $hostAddress,
            "-Port", ([string]$harnessUri.Port)
        )
        $harnessProcess = Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList $harnessArgs `
            -RedirectStandardOutput $harnessStdout -RedirectStandardError $harnessStderr -PassThru
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Milliseconds 500
            if (Test-HarnessReady $HarnessUrl) { $ready = $true; break }
            if ($harnessProcess.HasExited) { break }
        }
        if (-not $ready) {
            $details = @()
            if (Test-Path -LiteralPath $harnessStderr) {
                $details += Get-Content -LiteralPath $harnessStderr -Tail 30
            }
            if (-not $details -and (Test-Path -LiteralPath $harnessStdout)) {
                $details += Get-Content -LiteralPath $harnessStdout -Tail 30
            }
            $detailText = if ($details) { "`nHarness 原始输出：`n" + ($details -join "`n") } else { "" }
            throw "Harness 启动失败或未在 30 秒内通过 v1 健康检查（URL=$HarnessUrl，PID=$($harnessProcess.Id)）。日志：$harnessStderr$detailText"
        }
        Write-Host "独立 Harness 已启动：$HarnessUrl"
    } else {
        Write-Host "独立 Harness 已在运行：$HarnessUrl"
    }
} elseif (-not $SkipHarness) {
    Write-Host "HarnessUrl 不是本机地址，跳过自动启动：$HarnessUrl"
}

Assert-HarnessInstance $HarnessUrl $HarnessRoot

$registerScript = Join-Path $PSScriptRoot "register-harness-agent.ps1"
if (-not $SkipHarness -and (Test-Path -LiteralPath $registerScript)) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $registerScript -HarnessRoot $HarnessRoot -BaseUrl $HarnessUrl
    if ($LASTEXITCODE -ne 0) { throw "Harness Agent 注册/setup 失败" }
}

$env:AGENT_HARNESS_URL = $HarnessUrl
$env:AGENT_HARNESS_TASK_TOKEN = $TaskTokenLine.Split("=", 2)[1]
$PythonBin = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $PythonBin -m openagent_studio.app
