@echo off
chcp 65001 >nul
cd /d "%~dp0"
py backtest.py --config config_stocks_us.json --open %*
echo.
pause
