@echo off
title 重介密控系统 - 守护中（关闭本窗口即停止守护）
echo ============================================
echo   重介密控系统 守护中：每分钟检查一次
echo   服务若被结束，会自动重新启动
echo   关闭本窗口 = 停止守护（服务本身继续运行）
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\watchdog_server.ps1"
pause
