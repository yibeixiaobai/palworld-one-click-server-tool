@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py
) else if exist "python.exe" (
  "python.exe" run.py
) else (
  py -3 run.py
)
if errorlevel 1 pause
