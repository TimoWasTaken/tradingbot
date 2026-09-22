@echo off
chcp 65001 >nul
cd /d "%~dp0"
py report.py --config config_stocks_se.json --open %*
echo.
pause
