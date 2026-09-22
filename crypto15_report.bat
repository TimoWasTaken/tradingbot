@echo off
chcp 65001 >nul
cd /d "%~dp0"
py crypto15.py report --open
py crypto15.py research
pause
