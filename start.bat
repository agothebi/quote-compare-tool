@echo off
rem Quote Compare for Windows. Double-click this file.
rem The first start installs what the app needs (a few minutes); later starts take seconds.
setlocal
cd /d "%~dp0"
title Quote Compare

rem A .venv that no longer runs (the folder was moved, Python reinstalled) is made again.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || rmdir /s /q .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Quote Compare, first start only, a few minutes...
  py -3.13 -m venv .venv || goto nopython
)
".venv\Scripts\python.exe" app\ensure_deps.py || goto fail
".venv\Scripts\python.exe" -m app.launch %* || goto fail
exit /b 0

:nopython
rmdir /s /q .venv 2>nul
echo Python 3.13 is needed. See README.md, step 2, then start again.

:fail
echo.
pause
exit /b 1
