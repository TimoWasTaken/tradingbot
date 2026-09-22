@echo off
chcp 65001 >nul
cd /d "%~dp0"
py polymarket.py scan %*
echo.
pause
