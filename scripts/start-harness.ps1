param(
    [string]$HarnessRoot = "D:\Projects\my-harness",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeEnv = Join-Path $HarnessRoot ".runtime.env"
$HarnessBin = Join-Path $HarnessRoot ".venv\Scripts\agent-harness.exe"
if (-not (Test-Path -LiteralPath $HarnessBin)) { throw "Harness 未安装：$HarnessBin" }
if (-not (Test-Path -LiteralPath $RuntimeEnv)) { throw "Harness 运行时密钥文件不存在：$RuntimeEnv" }

Get-Content -LiteralPath $RuntimeEnv | ForEach-Object {
    if ($_ -and -not $_.StartsWith("#") -and $_.Contains("=")) {
        $Name, $Value = $_.Split("=", 2)
        # Windows PowerShell 5.1 preserves the UTF-8 BOM emitted by
        # Set-Content on the first line; remove it before exporting the key.
        $cleanName = $Name.Trim().Trim([char]0xFEFF)
        [Environment]::SetEnvironmentVariable($cleanName, $Value.Trim(), "Process")
    }
}
$env:AGENT_HARNESS_HOME = Join-Path $HarnessRoot "state"
$env:AGENT_HARNESS_ALLOWED_ROOTS = $ProjectRoot
& $HarnessBin serve --manifests (Join-Path $HarnessRoot "manifests") --host $HostAddress --port $Port
