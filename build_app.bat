@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Building BlocketDealFinder.exe (single file, no console window) ...
py -m pip install --quiet pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name BlocketDealFinder --paths . --exclude-module pandas --exclude-module numpy --exclude-module yfinance --exclude-module matplotlib --exclude-module scipy --exclude-module PIL app\blocket_app.py
echo.
echo Done: dist\BlocketDealFinder.exe
pause
