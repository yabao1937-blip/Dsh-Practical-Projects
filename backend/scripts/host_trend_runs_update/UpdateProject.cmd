@echo off
setlocal
chcp 65001 >nul
set "DMCS_COMBINED_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_COMBINED_ROOT%\offline-install.json" (
  if exist "D:\DshDensityControl\offline-install.json" set "DMCS_COMBINED_ROOT=D:\DshDensityControl"
)
if not exist "%DMCS_COMBINED_ROOT%\offline-install.json" (
  echo The installed DshDensityControl application was not found for this Windows user.
  pause
  exit /b 1
)
if not exist "%~dp0bundle.json" (
  echo Please extract the entire ZIP before running this file.
  pause
  exit /b 1
)
"%DMCS_COMBINED_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0update_trend_runs.py" --install-dir "%DMCS_COMBINED_ROOT%"
set "DMCS_COMBINED_RESULT=%ERRORLEVEL%"
pause
exit /b %DMCS_COMBINED_RESULT%
