@echo off
chcp 65001 >nul
rem 双击即可带守护启动后端（崩溃自动拉起，日志在 logs\backend-8000-<日期>.log）。
rem 可选参数透传：start-backend.cmd -Port 8010
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend-supervisor.ps1" %*
echo.
echo 守护已退出。日志目录：%~dp0..\logs
pause
