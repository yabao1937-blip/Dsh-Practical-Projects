@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0move_storage.py" (
  echo Extract the COMPLETE storage migration ZIP into a new folder first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0MoveStorageToD.ps1"
pause
