@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot Crypto15
echo Starting the crypto 15-minute markets watcher (paper). Close the window or press Ctrl+C to stop.
echo.
py crypto15.py run %*
echo.
pause
