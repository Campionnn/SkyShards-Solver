@echo off
setlocal
cd /d "%~dp0"
title SkyShards local solver

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Python 3.10 or newer was not found.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH" in the installer, then run this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"

set "NEED_INSTALL=1"
if exist ".venv\requirements.installed" (
  fc /b requirements.txt .venv\requirements.installed >nul 2>&1 && set "NEED_INSTALL=0"
)
if "%NEED_INSTALL%"=="1" (
  echo Installing dependencies ^(first run only, this can take a minute^)...
  "%VPY%" -m pip install --upgrade pip >nul 2>&1
  "%VPY%" -m pip install -r requirements.txt || goto :fail
  copy /y requirements.txt .venv\requirements.installed >nul
)

"%VPY%" server.py
echo.
echo The solver has stopped.
pause
exit /b 0

:fail
echo.
echo Something went wrong ^(see the messages above^).
pause
exit /b 1
