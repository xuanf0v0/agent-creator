param(
    [string]$HarnessRoot = "D:\Projects\my-harness",
    [string]$BaseUrl = "http://127.0.0.1:8765"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeEnv = Join-Path $HarnessRoot ".runtime.env"
$AdapterBin = Join-Path $HarnessRoot ".venv\Scripts\openagent-harness-opencode.exe"
$PythonBin = Join-Path $HarnessRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $RuntimeEnv)) { throw "Harness 运行时密钥文件不存在：$RuntimeEnv" }

$Secrets = @{}
Get-Content -LiteralPath $RuntimeEnv | ForEach-Object {
    if ($_ -and -not $_.StartsWith("#") -and $_.Contains("=")) {
        $Name, $Value = $_.Split("=", 2)
        $cleanName = $Name.Trim().Trim([char]0xFEFF)
        $Secrets[$cleanName] = $Value.Trim()
    }
}
if (-not $Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN) { throw "缺少 Harness 管理 Token" }
if (-not $Secrets.AGENT_HARNESS_TASK_TOKEN) { throw "缺少 Harness 任务 Token" }

$Manifest = @{
    id = "coding"
    name = "OpenCode Text Coding Agent"
    description = "Independent no-tools OpenCode text adapter used by OpenAgent Studio"
    labels = @{
        "runtime.example/implementation" = "openagent-harness-opencode"
        "runtime.example/model" = "deepseek/deepseek-v4-flash"
        "runtime.example/capability" = "text-generation"
        "runtime.example/sandbox" = "read-only"
    }
    cwd = $ProjectRoot
    env_file = (Join-Path $ProjectRoot ".env")
    task = @{
        command = @($AdapterBin, "--model", "deepseek/deepseek-v4-flash", "--agent", "openagent-runtime-text", "--env-file", (Join-Path $ProjectRoot ".env"))
        protocol = @{ kind = "stdin_json" }
        verification = @(@{ name = "adapter import"; command = @($PythonBin, "-c", "import openagent_harness_opencode"); timeout_seconds = 30 })
        tools = @{ allow = @("network"); ask = @(); deny = @("read", "write", "edit", "bash", "task", "destructive") }
        sandbox = @{ enabled = $true; backend = "auto"; enforcement = "best_effort"; network = "allow"; workspace_write = $false }
    }
}
$Headers = @{
    Authorization = "Bearer $($Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN)"
    "X-Harness-Supported-Versions" = "1"
}
$Existing = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/agents" -Headers $Headers
$Body = @{ manifest = $Manifest } | ConvertTo-Json -Depth 20
if ($Existing.id -contains "coding") {
    Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/v1/agents/coding" -Headers $Headers -ContentType "application/json" -Body $Body | Out-Null
} else {
    Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/agents" -Headers $Headers -ContentType "application/json" -Body $Body | Out-Null
}
$sha = [Security.Cryptography.SHA256]::Create()
$ManifestHash = [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Body))).ToLowerInvariant()
$sha.Dispose()
$SetupHeaders = @{
    Authorization = "Bearer $($Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN)"
    "Idempotency-Key" = "coding-setup-$ManifestHash"
}
Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/agents/coding/setup" -Headers $SetupHeaders | Out-Null
$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    $Status = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/agents/coding" -Headers $Headers
    if ($Status.lifecycle_state -eq "ready") {
        Write-Host "coding Agent 已注册到独立 Harness，环境状态已准备"
        $ready = $true
        break
    }
    if ($Status.lifecycle_state -eq "error") { throw "coding Agent setup 失败：$($Status.latest_setup.error_code)" }
    Start-Sleep -Milliseconds 500
}
if (-not $ready) { throw "coding Agent setup 未在 60 秒内 ready" }
$DescriptorHeaders = @{ Authorization = "Bearer $($Secrets.AGENT_HARNESS_TASK_TOKEN)"; "X-Harness-Supported-Versions" = "1" }
$Descriptor = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/task-agents/coding" -Headers $DescriptorHeaders
$labels = $Descriptor.labels
if (-not ($Descriptor.enabled -and $Descriptor.accepts_tasks -and $Descriptor.readiness.state -eq "ready" -and $Descriptor.protocol.kind -eq "stdin_json" -and $labels.'runtime.example/implementation' -eq "openagent-harness-opencode" -and $labels.'runtime.example/capability' -eq "text-generation")) {
    throw "coding task-agent descriptor 不匹配或未 ready"
}
