@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONPATH=%CD%\backend;%PYTHONPATH%"
echo Administrator console: http://127.0.0.1:8003
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn banking.admin_site:app --app-dir backend --host 127.0.0.1 --port 8003
) else (
  py -3.13 -m uvicorn banking.admin_site:app --app-dir backend --host 127.0.0.1 --port 8003
)
endlocal
