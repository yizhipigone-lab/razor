@echo off
cd /d "%~dp0"
echo Stopping P9 services...
venv313\Scripts\python stop_services.py
echo.
echo All services stopped.
pause
