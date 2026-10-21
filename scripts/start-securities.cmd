@echo off
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn banking.stock_site:app --app-dir backend --host 127.0.0.1 --port 8001
) else (
  py -3.13 -m uvicorn banking.stock_site:app --app-dir backend --host 127.0.0.1 --port 8001
)
pause
