@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot Swedish stocks
echo Starting the Swedish stocks bot (signals, paper trading). Close the window or press Ctrl+C to stop.
echo.
py run.py --config config_stocks_se.json %*
echo.
pause
