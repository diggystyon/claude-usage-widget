@echo off
setlocal
cd /d "%~dp0"

echo === Installing build deps (PyInstaller + runtime libs) ===
python -m pip install --upgrade pyinstaller >nul
python -m pip install -r requirements.txt
if errorlevel 1 goto :err

echo === Generating app.ico ===
python make_icon.py
if errorlevel 1 goto :err

echo === Building "Claude Usage.exe" (this takes ~30-60s) ===
REM Send the build's intermediate artifacts to %TEMP% so OneDrive sync
REM doesn't lock files in our project's build/ folder. The final exe
REM still lands in dist\Claude Usage.exe relative to this script.
set "WORK=%TEMP%\claude_usage_build"
if exist "%WORK%" rmdir /S /Q "%WORK%" 2>nul
if exist "build" rmdir /S /Q "build" 2>nul

python -m PyInstaller ^
  --noconsole ^
  --onefile ^
  --name "Claude Usage" ^
  --icon=app.ico ^
  --add-data "app.ico;." ^
  --hidden-import=win32crypt ^
  --hidden-import=cryptography.hazmat.primitives.ciphers.aead ^
  --hidden-import=winotify ^
  --collect-data certifi ^
  --workpath "%WORK%" ^
  --distpath "dist" ^
  --clean ^
  --noconfirm ^
  claude_usage_tray.py
if errorlevel 1 goto :err

echo.
echo Done. Your exe is at:
echo   %~dp0dist\Claude Usage.exe
echo.
echo Drop a shortcut to it into shell:startup to auto-launch with Windows.
echo run.bat will pick up the new exe automatically.
pause
exit /b 0

:err
echo.
echo Build failed - check the output above.
pause
exit /b 1
