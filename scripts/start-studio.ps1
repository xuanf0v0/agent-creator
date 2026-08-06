param(
    [string]$HarnessRoot = "D:\Projects\my-harness",
    [string]$HarnessUrl = "http://127.0.0.1:8765"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeEnv = Join-Path $HarnessRoot ".runtime.env"
$TaskTokenLine = Get-Content -LiteralPath $RuntimeEnv | Where-Object { $_.StartsWith("AGENT_HARNESS_TASK_TOKEN=") } | Select-Object -First 1
if (-not $TaskTokenLine) { throw "缺少 Harness 任务 Token" }
$env:AGENT_HARNESS_URL = $HarnessUrl
$env:AGENT_HARNESS_TASK_TOKEN = $TaskTokenLine.Split("=", 2)[1]
$PythonBin = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $PythonBin -m openagent_studio.app
