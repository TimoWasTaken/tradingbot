@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot funds
echo Starting the fund rotation bot (paper trading). Close the window or press Ctrl+C to stop.
echo.
py rotation.py run %*
echo.
pause
