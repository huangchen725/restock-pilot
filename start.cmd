@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Please run setup.cmd first.
  if "%~1"=="" pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0start.py" %*
set "RESTOCK_EXIT=%errorlevel%"
if not "%RESTOCK_EXIT%"=="0" if "%~1"=="" pause
exit /b %RESTOCK_EXIT%
