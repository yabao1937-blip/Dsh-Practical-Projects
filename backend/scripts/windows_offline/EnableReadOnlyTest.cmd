@echo off
setlocal
cd /d "%~dp0"
set "DMCS_INSTALL=%USERPROFILE%\DshDensityControl"
if not exist "readonly-update.json" goto missing
if not exist "access-config.json" goto missing
if not exist "%DMCS_INSTALL%\offline-install.json" goto installed_missing
if not exist "%DMCS_INSTALL%\backend\.venv\Scripts\python.exe" goto installed_missing
"%DMCS_INSTALL%\backend\.venv\Scripts\python.exe" -X utf8 "%~dp0enable_readonly_bridge.py" --install-dir "%DMCS_INSTALL%" --access-config "%~dp0access-config.json"
set "DMCS_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %DMCS_RESULT%
:missing
echo Extract the entire ReadOnlyBridge.zip into a new folder before running this file.
pause
exit /b 1
:installed_missing
echo Original installed project was not found under this Windows user profile.
pause
exit /b 1
