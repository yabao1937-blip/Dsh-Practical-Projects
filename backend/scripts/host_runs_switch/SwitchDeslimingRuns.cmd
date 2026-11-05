@echo off
setlocal
chcp 65001 >nul
set "DMCS_SWITCH_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_SWITCH_ROOT%\offline-install.json" (
  if exist "D:\DshDensityControl\offline-install.json" set "DMCS_SWITCH_ROOT=D:\DshDensityControl"
)
if not exist "%DMCS_SWITCH_ROOT%\offline-install.json" (
  echo Recognized DshDensityControl host installation was not found. No change was made.
  pause
  exit /b 1
)
if not exist "%~dp0bundle.json" (
  echo Extract the entire RUNS switch ZIP before running this file.
  pause
  exit /b 1
)
"%DMCS_SWITCH_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0switch_runs.py" --install-dir "%DMCS_SWITCH_ROOT%" %*
set "DMCS_SWITCH_RESULT=%ERRORLEVEL%"
pause
exit /b %DMCS_SWITCH_RESULT%
