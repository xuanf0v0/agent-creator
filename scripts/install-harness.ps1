param(
    [string]$HarnessRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $HarnessRoot) { $HarnessRoot = Join-Path $PSScriptRoot "..\my-harness" }
$VersionScript = Join-Path $PSScriptRoot "harness-version.ps1"
. $VersionScript
$PinnedRef = $HarnessPinnedRef
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
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Harness ZIP 下载/安装失败，改用同一提交的 Git 传输重试"
    $GitRuntimePackage = "agent-harness @ git+https://github.com/xuanf0v0/my-harness.git@$PinnedRef"
    uv pip install --python $PythonPath --link-mode copy $GitRuntimePackage $AdapterPath
    if ($LASTEXITCODE -ne 0) { throw "Harness 安装失败（ZIP 与 Git 传输均失败，exit=$LASTEXITCODE）" }
}
$PinnedRef | Set-Content -LiteralPath (Join-Path $HarnessRoot ".installed-ref") -Encoding ascii

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
Write-Host "Harness 版本：$PinnedRef"
