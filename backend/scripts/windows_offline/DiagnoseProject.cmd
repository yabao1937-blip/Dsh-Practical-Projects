@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "DMCS_DIAG_ROOT=%USERPROFILE%\DshDensityControl"
if not exist "%DMCS_DIAG_ROOT%\offline-install.json" (
  echo Installed DshDensityControl was not found for this Windows user.
  pause
  exit /b 1
)
"%DMCS_DIAG_ROOT%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0diagnose_project.py" --install-dir "%DMCS_DIAG_ROOT%"
pause
