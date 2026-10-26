@echo off
setlocal
chcp 65001 >nul
rem Install root detection: original per-user location first, then D:\ after a full move.
set "DMCS_UPDATE_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_UPDATE_ROOT%\offline-install.json" (
  if exist "D:\DshDensityControl\offline-install.json" (
    set "DMCS_UPDATE_ROOT=D:\DshDensityControl"
  )
)
if not exist "%~dp0update.json" (
  echo Please extract the entire update ZIP before running this file.
  pause
  exit /b 1
)
if not exist "%DMCS_UPDATE_ROOT%\offline-install.json" (
  echo The installed DshDensityControl application was not found for this Windows user.
  pause
  exit /b 1
)
"%DMCS_UPDATE_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0apply_update.py" --install-dir "%DMCS_UPDATE_ROOT%"
pause
