@echo off
chcp 65001 >nul
cd /d "%~dp0"
py copytrade.py report --open
pause
