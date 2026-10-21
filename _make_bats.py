# -*- coding: utf-8 -*-
"""生成 GBK 编码的 .bat 启动脚本。

cmd 默认按代码页 936 解析批处理，UTF-8 中文会乱码并导致命令被拆碎，
所以 .bat 必须以 GBK + CRLF 写盘。改动启动逻辑后运行本脚本重新生成。

用法：python _make_bats.py
"""
from pathlib import Path

APP = Path(__file__).resolve().parent

start_bat = """@echo off
title CNKI 批量下载器
setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
if not exist "%ROOT%app.py" (
    echo 未找到 app.py，请把本脚本放在项目根目录下运行。
    pause >nul
    exit /b 1
)

rem 解释器优先级：CNKI_PYTHON 环境变量 > python_path.txt > 项目内 .venv > PATH 上的 pythonw / python
set "PY=%CNKI_PYTHON%"
if not defined PY if exist "%ROOT%python_path.txt" set /p PY=<"%ROOT%python_path.txt"
if not defined PY if exist "%ROOT%.venv\\Scripts\\pythonw.exe" set "PY=%ROOT%.venv\\Scripts\\pythonw.exe"
if not defined PY if exist "%ROOT%.venv\\Scripts\\python.exe" set "PY=%ROOT%.venv\\Scripts\\python.exe"
if not defined PY (
    where pythonw >nul 2>nul
    if errorlevel 1 (set "PY=python") else (set "PY=pythonw")
)

rem 关键系统工具走绝对路径，避免 PATH 被其它软件（Git/MSYS 等）抢占同名命令
set "CURL=%SystemRoot%\\System32\\curl.exe"
if not exist "%CURL%" set "CURL=curl"
set "SLEEP=%SystemRoot%\\System32\\timeout.exe"

set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%"

"%CURL%" -s --noproxy "*" -m 2 "%URL%/api/state" 2>nul | findstr /c:"items" >nul
if !errorlevel! equ 0 goto openwin

echo 正在启动本地服务，请稍候...
start "" /b "%PY%" "%ROOT%app.py"

set /a N=0
:wait
"%SLEEP%" /t 1 /nobreak >nul 2>nul
"%CURL%" -s --noproxy "*" -m 2 "%URL%/api/state" 2>nul | findstr /c:"items" >nul
if !errorlevel! equ 0 goto openwin
set /a N+=1
if !N! lss 25 goto wait
echo.
echo 服务启动失败：请确认已执行 pip install -r requirements.txt 和 playwright install chromium，
echo 或用环境变量 CNKI_PYTHON 指定 python.exe 的完整路径。
pause >nul
exit /b 1

:openwin
set "EDGE=%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe"
if exist "%EDGE%" (
    start "" "%EDGE%" --app="%URL%" --window-size=1340,900 --window-position=120,60
) else (
    start "" "%URL%"
)
exit /b 0
"""

stop_bat = """@echo off
title 停止 CNKI 下载器服务
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo 已停止本地服务（端口 8765）。
timeout /t 2 >nul
exit /b 0
"""

targets = [
    (APP / "启动CNKI下载器.bat", start_bat),
    (APP / "停止服务.bat", stop_bat),
]

for p, text in targets:
    p.write_bytes(text.replace("\n", "\r\n").encode("gbk"))
    print("OK", p.name, p.stat().st_size, "bytes")
