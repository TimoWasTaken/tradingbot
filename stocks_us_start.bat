@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot US stocks
echo Starting the US stocks bot (signals, paper trading). Close the window or press Ctrl+C to stop.
echo.
py run.py --config config_stocks_us.json %*
echo.
pause
