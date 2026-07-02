<string>:15: SyntaxWarning: invalid escape sequence '\S'
@echo off
cd /d "%~dp0"
echo ============================================
echo   P9 量化平台 - 启动所有服务 (v5.4)
echo ============================================
echo.

echo [1/3] 启动后端 API 服务 (main.py:8888)...
netstat -ano | findstr ":8888 " | findstr LISTENING >/dev/null
if errorlevel 1 (
    start "P9-API" cmd /k "venv313\Scripts\python.exe main.py"
    echo   [OK] API 服务正在启动...
) else (
    echo   [--] 8888 端口已占用，跳过启动
)

echo.
echo [2/3] 启动实盘交易服务 (live_trader:8001)...
netstat -ano | findstr ":8001 " | findstr LISTENING >/dev/null
if errorlevel 1 (
    start "P9-LiveTrader" cmd /k "set QMT_ACCOUNT_ID=180056133 && set PYTHONIOENCODING=utf-8 && venv313\Scripts\python.exe -m uvicorn app.live_trader.main:app --host 127.0.0.1 --port 8001"
    echo   [OK] live_trader 正在启动 (预计 5-10 秒)
) else (
    echo   [--] 8001 端口已占用，跳过启动
)

echo.
echo [3/3] 等待服务就绪...
timeout /t 8 /nobreak >/dev/null

echo.
echo 正在打开前端页面...
start http://localhost:8888

echo.
echo ============================================
echo 服务已启动：
echo   - 前端页面: http://localhost:8888
echo   - 实盘交易: http://localhost:8001/live/health
echo   - API 健康检查: http://localhost:8888/health
echo.
echo 提示：
echo   1. 关闭 "P9-API" 窗口可停止后端服务
echo   2. 关闭 "P9-LiveTrader" 窗口可停止实盘服务
echo   3. 运行 stop.bat 停止所有服务
echo   4. 自选股价格加载慢属正常 (5552 只股票)
echo ============================================
echo.
echo 按任意键关闭此窗口...
pause

