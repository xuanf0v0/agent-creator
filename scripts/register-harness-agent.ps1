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
        $Secrets[$Name.Trim()] = $Value.Trim()
    }
}
if (-not $Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN) { throw "缺少 Harness 管理 Token" }

$Manifest = @{
    id = "coding"
    name = "OpenCode Coding Agent"
    description = "Independent OpenCode adapter used by OpenAgent Studio"
    cwd = $ProjectRoot
    env_file = (Join-Path $ProjectRoot ".env")
    task = @{
        command = @($AdapterBin, "--model", "deepseek/deepseek-v4-flash", "--agent", "plan", "--env-file", (Join-Path $ProjectRoot ".env"))
        protocol = @{ kind = "stdin_json" }
        verification = @(@{ name = "adapter import"; command = @($PythonBin, "-c", "import openagent_harness_opencode"); timeout_seconds = 30 })
        tools = @{ allow = @("read", "network"); ask = @(); deny = @("write", "edit", "destructive") }
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
$SetupHeaders = @{
    Authorization = "Bearer $($Secrets.AGENT_HARNESS_MANAGEMENT_TOKEN)"
    "Idempotency-Key" = "coding-setup-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
}
Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/agents/coding/setup" -Headers $SetupHeaders | Out-Null
Write-Host "coding Agent 已注册到独立 Harness，环境状态已准备"
