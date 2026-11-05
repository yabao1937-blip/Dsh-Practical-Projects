@echo off
setlocal
chcp 65001 >nul
set "DMCS_SOURCE_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_SOURCE_ROOT%\offline-install.json" (
  if exist "D:\DshDensityControl\offline-install.json" set "DMCS_SOURCE_ROOT=D:\DshDensityControl"
)
if not exist "%DMCS_SOURCE_ROOT%\offline-install.json" (
  echo The installed DshDensityControl application was not found for this Windows user.
  pause
  exit /b 1
)
"%DMCS_SOURCE_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0capture_sources.py" --install-dir "%DMCS_SOURCE_ROOT%"
set "DMCS_SOURCE_RESULT=%ERRORLEVEL%"
pause
exit /b %DMCS_SOURCE_RESULT%
