param(
    [string]$HarnessRoot = "",
    [string]$BaseUrl = "http://127.0.0.1:8765"
)

$ErrorActionPreference = "Stop"
if (-not $HarnessRoot) { $HarnessRoot = Join-Path $PSScriptRoot "..\my-harness" }
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

$Headers = @{
    Authorization = "Bearer $($Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN)"
    "X-Harness-Supported-Versions" = "1"
}
$Existing = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/agents" -Headers $Headers
$Definitions = @(
    @{ id = "coding"; name = "OpenCode Text Coding Agent"; description = "Independent no-tools OpenCode text adapter used by OpenAgent Studio"; agent = "openagent-runtime-text"; capability = "text-generation"; allow = @("network"); deny = @("read", "write", "edit", "bash", "task", "destructive") },
    @{ id = "repository-analysis"; name = "OpenCode Repository Analysis Agent"; description = "Read-only repository analysis through OpenCode"; agent = "openagent-runtime-analysis"; capability = "repository-analysis"; allow = @("network", "read"); deny = @("write", "edit", "bash", "task", "destructive") },
    @{ id = "test-runner"; name = "OpenCode Read-only Test Agent"; description = "Runs declared tests without writing project files"; agent = "openagent-runtime-tests"; capability = "test-execution"; allow = @("network", "bash"); deny = @("read", "write", "edit", "task", "destructive") }
)
$DescriptorHeaders = @{ Authorization = "Bearer $($Secrets.AGENT_HARNESS_TASK_TOKEN)"; "X-Harness-Supported-Versions" = "1" }
foreach ($Definition in $Definitions) {
    $Manifest = @{
        id = $Definition.id
        name = $Definition.name
        description = $Definition.description
        labels = @{
            "runtime.example/implementation" = "openagent-harness-opencode"
            "runtime.example/model" = "deepseek/deepseek-v4-flash"
            "runtime.example/capability" = $Definition.capability
            "runtime.example/sandbox" = "read-only"
        }
        cwd = $ProjectRoot
        env_file = (Join-Path $ProjectRoot ".env")
        task = @{
            command = @($AdapterBin, "--model", "deepseek/deepseek-v4-flash", "--agent", $Definition.agent, "--env-file", (Join-Path $ProjectRoot ".env"))
            protocol = @{ kind = "stdin_json" }
            verification = @(@{ name = "adapter import"; command = @($PythonBin, "-c", "import openagent_harness_opencode"); timeout_seconds = 30 })
            tools = @{ allow = $Definition.allow; ask = @(); deny = $Definition.deny }
            sandbox = @{ enabled = $true; backend = "auto"; enforcement = "best_effort"; network = "allow"; workspace_write = $false }
        }
    }
    $Body = @{ manifest = $Manifest } | ConvertTo-Json -Depth 20
    if ($Existing.id -contains $Definition.id) {
        Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/v1/agents/$($Definition.id)" -Headers $Headers -ContentType "application/json" -Body $Body | Out-Null
    } else {
        Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/agents" -Headers $Headers -ContentType "application/json" -Body $Body | Out-Null
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $HashBytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Body))
        $ManifestHash = -join ($HashBytes | ForEach-Object { $_.ToString("x2") })
    } finally {
        $sha.Dispose()
    }
    $SetupHeaders = @{
        Authorization = "Bearer $($Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN)"
        "Idempotency-Key" = "$($Definition.id)-setup-$ManifestHash"
    }
    Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/agents/$($Definition.id)/setup" -Headers $SetupHeaders | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        $Status = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/agents/$($Definition.id)" -Headers $Headers
        if ($Status.lifecycle_state -eq "ready") { $ready = $true; break }
        if ($Status.lifecycle_state -eq "error") { throw "$($Definition.id) Agent setup 失败：$($Status.latest_setup.error_code)" }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "$($Definition.id) Agent setup 未在 60 秒内 ready" }
    $Descriptor = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/task-agents/$($Definition.id)" -Headers $DescriptorHeaders
    $labels = $Descriptor.labels
    if (-not ($Descriptor.enabled -and $Descriptor.accepts_tasks -and $Descriptor.readiness.state -eq "ready" -and $Descriptor.protocol.kind -eq "stdin_json" -and $labels.'runtime.example/implementation' -eq "openagent-harness-opencode" -and $labels.'runtime.example/capability' -eq $Definition.capability)) {
        throw "$($Definition.id) task-agent descriptor 不匹配或未 ready"
    }
    Write-Host "$($Definition.id) Agent 已注册到独立 Harness，环境状态已准备"
}
