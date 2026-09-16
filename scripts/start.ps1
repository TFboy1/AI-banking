param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
& uv sync --frozen --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }

if (-not $SkipBuild -or -not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    Push-Location -LiteralPath 'frontend'
    try {
        & npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally {
        Pop-Location
    }
}
Write-Host '打开 http://127.0.0.1:8000 即可使用。有数只监听本机地址。'
Write-Host '按 Ctrl+C 停止服务；修改 .env 后重启服务使配置生效。'
& uv run --frozen uvicorn banking.main:app --host 127.0.0.1 --port 8000
