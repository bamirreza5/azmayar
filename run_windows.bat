@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "QA_PYTHON=python") else (set "QA_PYTHON=py -3")
if not exist ".venv\Scripts\python.exe" (
  %QA_PYTHON% -m venv .venv
  if errorlevel 1 goto :error
)
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ is required'"
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo Could not start Azmayar. Read the error above and README_FA.md.
echo Required: Python 3.11+, Git, Docker Desktop in Linux containers mode.
pause
exit /b 1
