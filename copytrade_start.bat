@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot Copytrade
echo Starting the copy-trading test (paper). Close the window or press Ctrl+C to stop.
echo.
py copytrade.py run %*
echo.
pause
