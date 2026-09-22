@echo off
chcp 65001 >nul
cd /d "%~dp0"
py report.py --open %*
echo.
pause
