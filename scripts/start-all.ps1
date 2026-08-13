param(
    [string]$HarnessRoot = "",
    [string]$HarnessUrl = "http://127.0.0.1:8765",
    [switch]$ForceInstall,
    [int]$FrontendPort = 5173,
    [string]$GeneratorMode = ""
)

$ErrorActionPreference = "Stop"
if (-not $HarnessRoot) { $HarnessRoot = Join-Path $PSScriptRoot "..\my-harness" }
# 解析为规范路径，避免 `..` 导致 Stop-ExpectedHarness 的进程命令行匹配失败
#（进程实际以 python.exe 启动、命令行里是已解析的 my-harness 路径）
$HarnessRoot = (Resolve-Path -LiteralPath $HarnessRoot).Path
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
    if (([Uri]$HarnessUrl).Host -in @("127.0.0.1", "localhost", "::1")) {
        Stop-ExpectedHarness ([Uri]$HarnessUrl) $HarnessRoot
    }
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $ProjectRoot "docker-compose.n8n.yml"
$StudioUrl = "http://127.0.0.1:8787"
$N8nUrl = "http://127.0.0.1:5678"

function Get-PortListeners([int]$Port) {
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Stop-ProjectPort([int]$Port, [string[]]$RequiredTerms) {
    $listeners = Get-PortListeners $Port
    if (-not $listeners) { return }
    $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($processId in $processIds) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
        $processTree = @()
        for ($depth = 0; $process -and $depth -lt 4; $depth++) {
            $processTree += $process
            if (-not $process.ParentProcessId) { break }
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)"
        }
        $commandLine = ($processTree | ForEach-Object { [string]$_.CommandLine }) -join "`n"
        $matches = @($RequiredTerms | Where-Object { $commandLine.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
        if ($matches.Count -ne $RequiredTerms.Count) {
            throw "端口 $Port 被未知进程占用（PID=$processId，命令行=$commandLine）；拒绝自动结束"
        }
    }
    foreach ($processId in $processIds) {
        Stop-Process -Id $processId -Force
        Write-Host "已停止项目旧进程：port=$Port PID=$processId"
    }
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if (-not (Get-PortListeners $Port)) { return }
        Start-Sleep -Milliseconds 100
    }
    throw "停止项目旧进程后，端口 $Port 未在 5 秒内释放"
}

function Wait-Http([string]$Url, [int]$Attempts = 60) {
    $lastError = "无响应"
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
            $lastError = "HTTP $($response.StatusCode)"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 500
    }
    throw "服务未通过健康检查：$Url（最后错误：$lastError）"
}

if (-not (Test-Path -LiteralPath $ComposeFile)) { throw "找不到 n8n Compose 文件：$ComposeFile" }
if ($FrontendPort -lt 1 -or $FrontendPort -gt 65535) { throw "FrontendPort 无效：$FrontendPort" }

Stop-ProjectPort $FrontendPort @("frontend", "node", $ProjectRoot)
Stop-ProjectPort 8787 @("openagent_studio", $ProjectRoot)

$n8nContainer = (& docker compose -f $ComposeFile ps -q n8n).Trim()
$n8nListeners = Get-PortListeners 5678
if ($n8nListeners) {
    if (-not $n8nContainer) { throw "端口 5678 被未知进程或容器占用；拒绝自动结束" }
    $container = @(& docker inspect $n8nContainer | ConvertFrom-Json)[0]
    $workingDirectory = [string]$container.Config.Labels.'com.docker.compose.project.working_dir'
    $composeService = [string]$container.Config.Labels.'com.docker.compose.service'
    if ($workingDirectory -ne $ProjectRoot -or $composeService -ne "n8n") {
        throw "端口 5678 不是当前 Compose 项目 n8n 占用：$workingDirectory|$composeService"
    }
    & docker compose -f $ComposeFile down
    if ($LASTEXITCODE -ne 0) { throw "停止旧 n8n 容器失败" }
}

Write-Host "启动 n8n：$N8nUrl"
& docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) { throw "n8n 启动失败（exit=$LASTEXITCODE）" }
Wait-Http "$N8nUrl/healthz"
Write-Host "n8n 健康检查通过：$N8nUrl"

$studioScript = Join-Path $PSScriptRoot "start-studio.ps1"
$studioLogRoot = Join-Path $ProjectRoot ".harness"
New-Item -ItemType Directory -Path $studioLogRoot -Force | Out-Null
$env:OPENAGENT_KILL_PORT = "0"
$studioArgs = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $studioScript,
    "-HarnessRoot", $HarnessRoot, "-HarnessUrl", $HarnessUrl
)
if ($GeneratorMode) { $studioArgs += @("-GeneratorMode", $GeneratorMode) }
$studioProcess = Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList $studioArgs -WorkingDirectory $ProjectRoot -RedirectStandardOutput (Join-Path $studioLogRoot "start-studio.stdout.log") -RedirectStandardError (Join-Path $studioLogRoot "start-studio.stderr.log") -PassThru
Wait-Http "$StudioUrl/api/spec"
Write-Host "Studio 健康检查通过：$StudioUrl (PID=$($studioProcess.Id))"

& (Join-Path $PSScriptRoot "import-n8n-workflows.ps1") -ComposeFile $ComposeFile
if ($LASTEXITCODE -ne 0) { throw "n8n 示例工作流导入/发布失败（exit=$LASTEXITCODE）" }

$frontendProcess = Start-Process -FilePath npm.cmd -WorkingDirectory (Join-Path $ProjectRoot "frontend") -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", ([string]$FrontendPort), "--strictPort") -RedirectStandardOutput (Join-Path $studioLogRoot "vite.stdout.log") -RedirectStandardError (Join-Path $studioLogRoot "vite.stderr.log") -PassThru
Wait-Http "http://127.0.0.1:$FrontendPort/"
Write-Host "前端健康检查通过：http://127.0.0.1:$FrontendPort (PID=$($frontendProcess.Id))"

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " OpenAgent Studio 全部服务已启动" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ("  Studio 后端     http://127.0.0.1:8787  (PID={0})" -f $studioProcess.Id)
Write-Host ("  前端界面        http://127.0.0.1:{0}   (PID={1})" -f $FrontendPort, $frontendProcess.Id)
Write-Host ("  独立 Harness    {0}" -f $HarnessUrl)
Write-Host ("  n8n 连接器      {0}" -f $N8nUrl)
Write-Host "------------------------------------------------------" -ForegroundColor Cyan
Write-Host "  打开浏览器访问前端：http://127.0.0.1:$FrontendPort"
Write-Host "  Studio API 文档：   http://127.0.0.1:8787/docs"
Write-Host "======================================================" -ForegroundColor Cyan
