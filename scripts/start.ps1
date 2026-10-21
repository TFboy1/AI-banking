param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
$useUv = $null -ne (Get-Command uv -ErrorAction SilentlyContinue)
if ($useUv) {
    & uv sync --frozen --extra speech --extra face --link-mode hardlink
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
} else {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw '未找到 uv 或 Python 启动器 py，请先安装 uv。'
    }
    & py -3 -c "import sys, fastapi, uvicorn, httpx, pydantic_settings, tzdata; assert (3,11) <= sys.version_info[:2] < (3,14)"
    if ($LASTEXITCODE -ne 0) { throw '未找到可用的 Python 3.11-3.13 或缺少后端依赖，请安装 uv 后重试。' }
    Write-Host '未安装 uv，使用已有 Python 和后端依赖启动。'
}

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
if ($useUv) {
    & uv run --frozen --extra speech --extra face uvicorn banking.main:app --host 127.0.0.1 --port 8000
} else {
    $env:PYTHONPATH = (Join-Path $projectDir 'backend') + [IO.Path]::PathSeparator + $env:PYTHONPATH
    & py -3 -m uvicorn banking.main:app --host 127.0.0.1 --port 8000
}
