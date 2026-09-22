@echo off
chcp 65001 >nul
cd /d "%~dp0"
py blocket.py report --open %*
echo.
pause
