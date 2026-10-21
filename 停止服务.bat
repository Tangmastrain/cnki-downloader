@echo off
title 停止 CNKI 下载器服务
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo 已停止本地服务（端口 8765）。
timeout /t 2 >nul
exit /b 0
