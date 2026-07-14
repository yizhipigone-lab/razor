@echo off
cd /d "%~dp0"
echo ============================================
echo   P9 量化平台 - 停止所有服务 （v6.0）
echo   修复: 按命令行兜底杀（回测脚本/卡死uvicorn/进程树）
echo ============================================
echo.

setlocal enabledelayedexpansion

echo [1/5] 停止 main.py （端口 8888）...
netstat -ano | findstr ":8888 " | findstr LISTENING >nul 2>&1
if errorlevel 1 (
    echo   [--] 8888 端口未占用
) else (
    echo   请求 /shutdown 优雅关闭...
    curl -s -m 5 -X POST http://127.0.0.1:8888/shutdown >nul 2>&1
    echo   等待 8s 优雅退出 （DuckDB checkpoint）...
    %SystemRoot%\system32\ping.exe -n 9 127.0.0.1 >nul
    netstat -ano | findstr ":8888 " | findstr LISTENING >nul 2>&1
    if errorlevel 1 (
        echo   [OK] 8888 服务已优雅退出
    ) else (
        echo   [警告] 优雅关闭超时, 兜底强杀...
        for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8888 " ^| findstr LISTENING') do (
            taskkill /F /T /PID %%a >nul 2>&1
        )
        echo   [OK] 8888 已强制停止
    )
)

echo.
echo [2/5] 停止 live_trader （端口 8001）...
netstat -ano | findstr ":8001 " | findstr LISTENING >nul 2>&1
if errorlevel 1 (
    echo   [--] 8001 端口未占用
) else (
    echo   请求 /shutdown 优雅关闭...
    curl -s -m 5 -X POST http://127.0.0.1:8001/shutdown >nul 2>&1
    echo   等待 8s 优雅退出 （DuckDB checkpoint）...
    %SystemRoot%\system32\ping.exe -n 9 127.0.0.1 >nul
    netstat -ano | findstr ":8001 " | findstr LISTENING >nul 2>&1
    if errorlevel 1 (
        echo   [OK] 8001 服务已优雅退出
    ) else (
        echo   [警告] 优雅关闭超时, 兜底强杀...
        for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 " ^| findstr LISTENING') do (
            taskkill /F /T /PID %%a >nul 2>&1
        )
        echo   [OK] 8001 已强制停止
    )
)

echo.
echo [3/5] 按命令行兜底杀项目 Python 进程 （核心修复）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_kill.ps1"

echo.
echo [4/5] 停止 Docker 容器 quant-api （如果运行）...
docker ps --filter "name=quant-api" --format "{{.Names}}" 2>nul | findstr "quant-api" >nul
if errorlevel 1 (
    echo   [--] Docker 容器未运行
) else (
    echo   尝试停止 Docker 容器 quant-api...
    docker stop quant-api >nul 2>&1
    if errorlevel 1 (
        echo   [警告] 停止失败, 尝试强制停止...
        docker kill quant-api >nul 2>&1
    ) else (
        echo   [OK] Docker 容器已停止
    )
)

echo.
echo [5/5] 清理 live_trader 锁文件...
if exist data\live_trader\live.lock (
    del /f data\live_trader\live.lock >nul 2>&1
    echo   [OK] 锁文件已删除
) else (
    echo   [--] 锁文件不存在, 无需清理
)

endlocal

echo.
echo ============================================
echo 所有停止步骤已完成
echo ============================================
echo.
echo 若窗口不再需要, 可以直接点击右上角 X 按钮关闭
echo.
cmd /k
