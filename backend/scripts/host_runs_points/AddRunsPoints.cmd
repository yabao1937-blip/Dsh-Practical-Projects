@echo off
setlocal
chcp 65001 >nul
set "DMCS_RUNS_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_RUNS_ROOT%\offline-install.json" (
  if exist "D:\DshDensityControl\offline-install.json" set "DMCS_RUNS_ROOT=D:\DshDensityControl"
)
if not exist "%DMCS_RUNS_ROOT%\offline-install.json" (
  echo Recognized DshDensityControl installation was not found. No change was made.
  pause
  exit /b 1
)
if not exist "%~dp0bundle.json" (
  echo Extract the entire RUNS ZIP before running this file.
  pause
  exit /b 1
)
"%DMCS_RUNS_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0add_runs_points.py" --install-dir "%DMCS_RUNS_ROOT%" %*
set "DMCS_RUNS_RESULT=%ERRORLEVEL%"
pause
exit /b %DMCS_RUNS_RESULT%
