@echo off
cd /d "%~dp0"
echo ============================================
echo   P9 量化平台 - 停止所有服务
echo ============================================
echo.

setlocal enabledelayedexpansion

echo [1/2] 正在停止后端 API 服务（端口 8888）...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 " ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1 && set KILLED=1
)
if !KILLED!==1 ( echo   [OK] 服务已停止 ) else ( echo   [--] 服务未运行 )

echo [2/2] 正在停止 QMT 代理服务（端口 8081）...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8081 " ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1 && set KILLED=1
)
if !KILLED!==1 ( echo   [OK] 服务已停止 ) else ( echo   [--] 服务未运行 )

endlocal
echo.
echo ============================================
echo 服务已停止，不影响其他 Python 进程和数据库。
echo ============================================
echo.
pause
