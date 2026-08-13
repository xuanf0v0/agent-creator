param(
    [string]$ComposeFile = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "docker-compose.n8n.yml")
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkflowRoot = Join-Path $ProjectRoot "n8n\workflows"
$ExpectedVersion = "2.34.4"
$Workflows = @(
    @{ File = "studio-fetch-sheet.json"; Id = "studioFetchSheet1" },
    @{ File = "studio-callback-test.json"; Id = "studioCallback1" }
)

function Invoke-N8n([string[]]$Arguments) {
    & docker compose -f $ComposeFile exec -T -u node n8n n8n @Arguments
    if ($LASTEXITCODE -ne 0) { throw "n8n 命令失败（exit=$LASTEXITCODE）：n8n $($Arguments -join ' ')" }
}

if (-not (Test-Path -LiteralPath $ComposeFile)) { throw "找不到 Compose 文件：$ComposeFile" }
if (-not (Test-Path -LiteralPath $WorkflowRoot)) { throw "找不到工作流目录：$WorkflowRoot" }
$version = (& docker compose -f $ComposeFile exec -T n8n n8n --version).Trim()
if ($LASTEXITCODE -ne 0 -or $version -ne $ExpectedVersion) {
    throw "n8n 版本不匹配：需要 $ExpectedVersion，实际 $version"
}
$containerId = (& docker compose -f $ComposeFile ps -q n8n).Trim()
if (-not $containerId) { throw "n8n 容器未运行" }

foreach ($workflow in $Workflows) {
    $path = Join-Path $WorkflowRoot $workflow.File
    if (-not (Test-Path -LiteralPath $path)) { throw "找不到工作流文件：$path" }
    $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ([string]$json.id -ne $workflow.Id) { throw "工作流 ID 不匹配：$path" }
    if ($json.PSObject.Properties.Name -contains "credentials") { throw "示例工作流不得包含 credentials：$path" }
    $containerPath = "/tmp/$($workflow.File)"
    & docker cp $path "${containerId}:$containerPath"
    if ($LASTEXITCODE -ne 0) { throw "无法复制工作流到 n8n 容器：$path" }
    Invoke-N8n @("import:workflow", "--input=$containerPath")
    Invoke-N8n @("publish:workflow", "--id=$($workflow.Id)")
    Write-Host "已导入并发布：$($workflow.File) ($($workflow.Id))"
}

& docker compose -f $ComposeFile restart n8n
if ($LASTEXITCODE -ne 0) { throw "发布工作流后重启 n8n 失败" }
$lastError = "无响应"
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:5678/healthz" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { return }
    } catch {
        $lastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 500
}
throw "发布工作流后 n8n 未在 30 秒内恢复健康（最后错误：$lastError）"
