@echo off
rem Start YT Automation Studio on Windows (run install\install-windows.ps1 once first).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run install\install-windows.ps1 to install everything.
  pause
  exit /b 1
)
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8765"
".venv\Scripts\python.exe" -m studio
