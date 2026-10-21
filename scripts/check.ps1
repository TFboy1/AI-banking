$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
& uv run ruff check backend
if ($LASTEXITCODE -ne 0) { throw '后端静态检查失败。' }
& uv run pytest -q
if ($LASTEXITCODE -ne 0) { throw '后端测试失败。' }
Push-Location -LiteralPath 'frontend'
try {
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw '前端类型检查或 build 失败。' }
} finally {
    Pop-Location
}
