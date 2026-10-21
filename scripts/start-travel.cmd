@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONPATH=%CD%\backend;%PYTHONPATH%
set PYTHONUTF8=1
echo 打开 http://127.0.0.1:8004 使用独立旅行演示站。请同时启动有数后台。
where uv >nul 2>nul
if %errorlevel% equ 0 (
  uv run --frozen uvicorn banking.travel_site:app --host 127.0.0.1 --port 8004
) else (
  py -3 -m uvicorn banking.travel_site:app --host 127.0.0.1 --port 8004
)
pause
