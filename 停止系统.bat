@echo off
chcp 65001 >nul
title 重介密控系统 - 停止
echo ============================================
echo   重介密控系统 停止中...
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\stop_server.ps1"
echo.
pause
