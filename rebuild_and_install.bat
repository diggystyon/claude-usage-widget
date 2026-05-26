@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   Claude Usage - rebuild + reinstall
echo ==========================================================

REM ----------------------------------------------------------
REM [1/3] Build Claude Usage.exe via PyInstaller
REM ----------------------------------------------------------
echo.
echo [1/3] Building "Claude Usage.exe" ...

python -m pip install --upgrade pyinstaller >nul
if errorlevel 1 goto :err

python -m pip install -r requirements.txt >nul
if errorlevel 1 goto :err

python make_icon.py
if errorlevel 1 goto :err

REM Route PyInstaller's intermediates to %TEMP% so OneDrive sync doesn't lock
REM files in our project's build/ folder.
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

REM ----------------------------------------------------------
REM [2/3] Build Claude Usage Setup.exe via Inno Setup
REM ----------------------------------------------------------
echo.
echo [2/3] Building "Claude Usage Setup.exe" ...

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 5\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 5\ISCC.exe"

if not defined ISCC (
  echo.
  echo Inno Setup not found.
  echo Install once from: https://jrsoftware.org/isdl.php
  goto :err
)

if not exist "dist\Claude Usage.exe" (
  echo.
  echo dist\Claude Usage.exe missing -- step 1 must have failed.
  goto :err
)

REM Read the version from _version.py (single source of truth) and pass
REM it to ISCC as /DAppVersion=... . installer.iss has a #ifndef fallback
REM for the dev-direct-invoke case, but we always want the real value.
for /f %%v in ('python -c "from _version import __version__; print(__version__)"') do set "APP_VERSION=%%v"
if not defined APP_VERSION (
  echo.
  echo Could not read __version__ from _version.py.
  goto :err
)
echo Using AppVersion=%APP_VERSION% from _version.py

"%ISCC%" /DAppVersion=%APP_VERSION% installer.iss
if errorlevel 1 goto :err

REM ----------------------------------------------------------
REM [3/3] Reinstall silently and relaunch the widget
REM ----------------------------------------------------------
echo.
echo [3/3] Reinstalling silently ...

REM Stop the running widget so the installer can replace its exe.
taskkill /IM "Claude Usage.exe" /F >nul 2>&1

REM Brief settle so the file handle is fully released.
timeout /t 1 /nobreak >nul

REM /VERYSILENT runs the installer headlessly; /SUPPRESSMSGBOXES auto-yes
REM any popups; /NORESTART avoids reboot prompts. installer.iss skips the
REM first-install Edge / Explorer / wizard-to-front dance when WizardSilent()
REM is true, so this is fast and unobtrusive.
"installer_dist\Claude Usage Setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
if errorlevel 1 goto :err

echo.
echo === Done. The widget should be running again. ===
pause
exit /b 0

:err
echo.
echo Step failed -- aborting.
pause
exit /b 1
