@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot sports arbitrage
echo Starting the sports arbitrage measurement. Close the window or press Ctrl+C to stop.
echo.
py sportsarb.py run %*
echo.
pause
