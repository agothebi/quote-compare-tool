@echo off
rem Quote Compare for Windows. Double-click this file.
rem First start: creates .venv and installs what the app needs. Then: starts the app and opens the browser.
setlocal
cd /d "%~dp0"
title Quote Compare

if exist ".venv\Scripts\python.exe" goto deps
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.11 or newer is needed. Install it from python.org and tick "Add python.exe to PATH", then start again.
  goto fail
)
echo Setting up Quote Compare (first start only)...
%PY% -m venv .venv
if errorlevel 1 (
  echo Could not create the Python environment.
  rmdir /s /q .venv 2>nul
  goto fail
)

:deps
".venv\Scripts\python.exe" tools\ensure_deps.py
if errorlevel 1 goto fail
set PYTHONUNBUFFERED=1
".venv\Scripts\python.exe" -m app.launch %*
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
pause
exit /b 1
