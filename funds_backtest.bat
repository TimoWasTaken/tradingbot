@echo off
chcp 65001 >nul
cd /d "%~dp0"
py rotation.py backtest --open %*
echo.
pause
