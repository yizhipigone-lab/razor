@echo off
cd /d "%~dp0"
echo ============================================
echo   P9 量化平台 - 启动所有服务
echo ============================================
echo.

echo [1/2] 启动后端 API 服务（端口 8888）...
start "P9-API" cmd /k "venv313\Scripts\python main.py"

echo [2/2] 启动 QMT 代理服务（端口 8081）...
start "P9-Proxy" cmd /k "venv313\Scripts\python qmt_proxy_server.py"

echo 正在等待服务启动...
timeout /t 3 /nobreak >nul

echo 正在打开前端首页...
start http://localhost:8888

echo.
echo 所有服务已启动，每个服务运行在独立窗口中。
echo 关闭对应窗口即可停止服务，或运行 stop.bat
echo.
pause
