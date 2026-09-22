@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Installing the Python packages the bots need ...
py -m pip install -r requirements.txt
echo.
echo Done. Press any key to close.
pause >nul
