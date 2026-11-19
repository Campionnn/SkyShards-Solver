@echo off
setlocal
cd /d "%~dp0"
title SkyShards local solver

rem OR-Tools (the solver) only ships for 64-bit x86 Python 3.10 to 3.14 on
rem Windows, so any other Python fails at "pip install". Find one that fits.
set "CHECK=import sys, struct, platform; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64') else 1)"

set "PY="
for %%V in (3.13 3.12 3.11 3.14 3.10) do call :try_py "py -%%V"
if not defined PY call :try_py "py -3"
if not defined PY call :try_py "python"
if not defined PY goto :no_python

rem A virtual environment made earlier with an unsupported Python can never
rem install the solver: start over with the one found above.
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" goto :make_venv
"%VPY%" -c "%CHECK%" >nul 2>&1 && goto :have_venv
echo Rebuilding the virtual environment with a supported Python...
rmdir /s /q ".venv"

:make_venv
echo Creating virtual environment...
%PY% -m venv .venv || goto :fail

:have_venv
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

:try_py
if defined PY exit /b 0
%~1 -c "%CHECK%" >nul 2>&1 && set "PY=%~1"
exit /b 0

:no_python
echo.
echo No supported Python was found.
echo.
echo The solver needs 64-bit Python 3.10 to 3.14. OR-Tools, the solver library,
echo has no build for Python 3.15 or newer yet, nor for 32-bit or ARM Python.
echo.
echo Install Python 3.13 (Windows installer, 64-bit) from
echo   https://www.python.org/downloads/windows/
echo It can sit next to the Python you already have. Then run this file again.
echo.
pause
exit /b 1

:fail
echo.
echo Something went wrong ^(see the messages above^).
pause
exit /b 1
