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
if not exist "%DMCS_UPDATE_ROOT%\backend\data\dense_medium.db" (
  echo Production database not found under "%DMCS_UPDATE_ROOT%\backend\data".
  pause
  exit /b 1
)
echo Backfill hourly advice for the last 7 days from stored evidence.
echo Web service and collector keep running; only gpt_hourly_density_advice rows are added.
echo Already archived hours are never touched. Add --dry-run to preview without writing.
"%DMCS_UPDATE_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0backfill_host.py" "%DMCS_UPDATE_ROOT%\backend" %*
pause
