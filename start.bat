@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot crypto
echo Starting the crypto bot. Close the window or press Ctrl+C to stop.
echo.
py run.py %*
echo.
pause
