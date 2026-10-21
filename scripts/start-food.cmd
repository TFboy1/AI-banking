@echo off
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn banking.food_site:app --app-dir backend --host 127.0.0.1 --port 8002
) else (
  py -3.13 -m uvicorn banking.food_site:app --app-dir backend --host 127.0.0.1 --port 8002
)
pause
