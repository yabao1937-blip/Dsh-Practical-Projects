@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "READER_PYTHON=%~dp0backend\.venv\Scripts\python.exe"
if not exist "%READER_PYTHON%" (
  echo Python environment not found: backend\.venv
  pause
  exit /b 1
)
"%READER_PYTHON%" "%~dp0backend\scripts\read_kep_node.py" %*
set "READER_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %READER_EXIT%
