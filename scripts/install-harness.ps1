param(
    [string]$HarnessRoot = "D:\Projects\my-harness"
)

$ErrorActionPreference = "Stop"
$PinnedRef = "bea70fb812e98f530d262eeccb5a889b51dc821d"
$RuntimePackage = "agent-harness @ https://github.com/xuanf0v0/my-harness/archive/$PinnedRef.zip"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AdapterPath = Join-Path $ProjectRoot "adapters\opencode"
$VenvPath = Join-Path $HarnessRoot ".venv"
$PythonPath = Join-Path $VenvPath "Scripts\python.exe"
$RuntimeEnv = Join-Path $HarnessRoot ".runtime.env"
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".cache\uv"

New-Item -ItemType Directory -Force -Path $HarnessRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $HarnessRoot "manifests") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $HarnessRoot "state") | Out-Null

if (-not (Test-Path -LiteralPath $PythonPath)) {
    uv venv $VenvPath --python 3.11
}
uv pip install --python $PythonPath --link-mode copy $RuntimePackage $AdapterPath

if (-not (Test-Path -LiteralPath $RuntimeEnv)) {
    function New-SecureToken {
        $Bytes = New-Object byte[] 32
        $Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
        return -join ($Bytes | ForEach-Object { $_.ToString("x2") })
    }
    $TaskToken = New-SecureToken
    $ManagementToken = New-SecureToken
    @(
        "AGENT_HARNESS_TASK_TOKEN=$TaskToken"
        "AGENT_HARNESS_MANAGEMENT_TOKEN=$ManagementToken"
    ) | Set-Content -LiteralPath $RuntimeEnv -Encoding utf8
}

Write-Host "Harness 已安装到 $HarnessRoot"
Write-Host "下一步：运行 scripts\start-harness.ps1，再运行 scripts\register-harness-agent.ps1"
