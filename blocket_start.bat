@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradingbot Blocket
echo Starting the Blocket deal finder. Close the window or press Ctrl+C to stop.
echo.
py blocket.py run %*
echo.
pause
