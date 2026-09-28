@echo off
chcp 65001 >nul
title 重介密控系统 - 启动
echo ============================================
echo   重介密控系统 启动中...
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\start_server.ps1"
echo.
echo 关闭这个窗口不会影响系统运行（服务是独立进程）。
pause
