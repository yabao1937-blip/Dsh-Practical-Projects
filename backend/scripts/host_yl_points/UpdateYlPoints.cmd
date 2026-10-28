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
if not exist "%DMCS_UPDATE_ROOT%\offline-install.json" (
  echo The installed DshDensityControl application was not found for this Windows user.
  pause
  exit /b 1
)
if not exist "%DMCS_UPDATE_ROOT%\backend\config\kepserver.local.json" (
  echo Collector configuration not found under "%DMCS_UPDATE_ROOT%\backend\config".
  pause
  exit /b 1
)
echo Point the A416/A417 press-state tags at their renamed OPC addresses.
echo The configuration is backed up, then only this installation's collector restarts.
echo Add --dry-run to preview without writing anything.
"%DMCS_UPDATE_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0update_yl_points.py" --install-dir "%DMCS_UPDATE_ROOT%" %*
pause
