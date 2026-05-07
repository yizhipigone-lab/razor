@echo off
cd /d "%~dp0"
echo Starting P9 Quant Platform...
start "P9-API" cmd /k "venv313\Scripts\python main.py"
start "P9-Proxy" cmd /k "venv313\Scripts\python qmt_proxy_server.py"
echo Both services started.
echo Close each window to stop, or run stop_all.bat
