@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Building BlocketDealFinder.exe (single file, no console window) ...
py -m pip install --quiet pyinstaller pystray pillow
rem bake the licence secret from secrets.json into the bundle (app\_secret.py is git-ignored)
py -c "import json; d=json.load(open('secrets.json',encoding='utf-8')); k=d.get('license_secret') or ''; open('app/_secret.py','w',encoding='utf-8').write('LICENSE_SECRET = %r\n' % (k if k and 'PASTE' not in k.upper() else 'dev-secret-not-for-sale')); print('licence secret:', 'from secrets.json' if k and 'PASTE' not in k.upper() else 'MISSING (dev secret used, keys from keygen will NOT match)')"
py -m PyInstaller --noconfirm --clean --onefile --windowed --name BlocketDealFinder --paths . --paths app --add-data "app\ui;ui" --hidden-import pystray._win32 --exclude-module pandas --exclude-module numpy --exclude-module yfinance --exclude-module matplotlib --exclude-module scipy app\blocket_app.py
if exist dist\BlocketDealFinder.exe (
  copy /y app\GUIDE_SV.md dist\LAS_MIG_FORST.md >nul
  echo.
  echo Done: dist\BlocketDealFinder.exe  (+ dist\LAS_MIG_FORST.md)
) else (
  echo Build failed, see the messages above.
)
pause
