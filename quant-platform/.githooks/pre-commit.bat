@echo off
REM pre-commit hook for Windows (Windows Git 优先匹配 .bat)
python "%~dp0pre-commit.py" %*
exit /b %ERRORLEVEL%
