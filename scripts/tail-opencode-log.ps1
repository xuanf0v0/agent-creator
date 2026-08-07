param(
    [string]$Path = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path ".openagent-logs\opencode.jsonl"),
    [int]$Tail = 50
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Write-Host "OpenCode 日志尚未生成：$Path"
    Write-Host "启动一次创建或优化任务后再运行本脚本。"
    exit 0
}

Write-Host "正在跟踪 OpenCode 日志：$Path"
Get-Content -LiteralPath $Path -Encoding UTF8 -Tail ([Math]::Max(1, $Tail)) -Wait
