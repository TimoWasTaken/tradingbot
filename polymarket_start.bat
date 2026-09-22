@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot Polymarket
echo Starting the Polymarket paper bettor. Close the window or press Ctrl+C to stop.
echo.
py polymarket.py run %*
echo.
pause
