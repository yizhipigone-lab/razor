<string>:61: SyntaxWarning: invalid escape sequence '\l'
@echo off
cd /d "%~dp0"
echo ============================================
echo   P9 量化平台 - 停止所有服务 (v5.4)
echo ============================================
echo.

setlocal enabledelayedexpansion

echo [1/3] 正在停止 main.py (端口 8888)...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 " ^| findstr LISTENING') do (
    echo   找到进程 PID: %%a，正在终止...
    taskkill /F /PID %%a
    if errorlevel 0 set KILLED=1
)
if !KILLED!==1 ( 
    echo   [OK] main.py 已停止
) else ( 
    echo   [--] 8888 端口未占用
)

echo.
echo [2/3] 正在停止 live_trader (端口 8001)...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 " ^| findstr LISTENING') do (
    echo   找到进程 PID: %%a，正在终止...
    taskkill /F /PID %%a
    if errorlevel 0 set KILLED=1
)
if !KILLED!==1 ( 
    echo   [OK] live_trader 已停止
) else ( 
    echo   [--] 8001 端口未占用
)

echo.
echo [3/3] 停止 Docker 容器 (如果在运行)...
docker ps --filter "name=quant-api" --format "{{.Names}}" 2>/dev/null | findstr "quant-api" >/dev/null
if errorlevel 1 (
    echo   [--] Docker 容器未运行
) else (
    echo   正在停止 Docker 容器 quant-api...
    docker stop quant-api
    if errorlevel 1 (
        echo   [警告] 停止失败，尝试强制停止...
        docker kill quant-api 2>/dev/null
    ) else (
        echo   [OK] Docker 容器已停止
    )
)

echo.
echo ============================================
echo [可选] 清理遗留文件
echo ============================================
if exist data\live_trader\live.lock (
    del /f data\live_trader\live.lock 2>/dev/null
    echo   [OK] 锁文件已删除
) else (
    echo   [--] 无锁文件需要清理
)

endlocal

echo.
echo ============================================
echo 所有服务已停止。
echo ============================================
echo.
echo 按任意键关闭此窗口...
pause

