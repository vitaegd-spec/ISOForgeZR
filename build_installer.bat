@echo off
REM ==========================================================================
REM  build_installer.bat -- builds the ISOForge(ZR) Windows installer.
REM
REM  Steps:
REM    1. Create/activate the project venv and install build dependencies
REM    2. Build the app with PyInstaller (ISOForgeZR.spec -> dist\ISOForgeZR)
REM    3. Read the version from app\__init__.py
REM    4. Compile installer\IsoForge.iss with Inno Setup (ISCC.exe)
REM
REM  Output: installer\Output\ISOForgeZR-Setup-<version>.exe
REM
REM  Requirements: Python 3.13 on PATH, Inno Setup 6.3+ installed
REM  (https://jrsoftware.org/isdl.php  or  winget install JRSoftware.InnoSetup)
REM
REM  Usage:  build_installer.bat            (normal build)
REM          build_installer.bat nopause    (no pause at the end, for scripts)
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_EXE=ISOForgeZR.exe"
set "SPEC=ISOForgeZR.spec"
set "ISS=installer\IsoForge.iss"
set "ICON=build\isoforge.ico"

echo.
echo ===== ISOForge(ZR) installer build =====
echo.

REM -- Pre-flight checks ------------------------------------------------------
if not exist "%SPEC%" (
    echo [ERROR] %SPEC% not found in %CD%
    goto :fail
)
if not exist "%ISS%" (
    echo [ERROR] %ISS% not found.
    goto :fail
)
if not exist "%ICON%" (
    echo [ERROR] %ICON% not found. The spec and installer both need it.
    goto :fail
)

REM PyInstaller cannot overwrite dist\ISOForgeZR while the app is running.
tasklist /FI "IMAGENAME eq %APP_EXE%" 2>nul | find /I "%APP_EXE%" >nul
if not errorlevel 1 (
    echo [ERROR] %APP_EXE% is running. Close ISOForge and try again.
    goto :fail
)

REM -- 1. Virtual environment + dependencies ---------------------------------
if not exist "venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Could not create the venv. Is Python on PATH?
        goto :fail
    )
) else (
    echo [1/4] Using existing virtual environment.
)
call "venv\Scripts\activate.bat"

python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Installing requirements failed.
    goto :fail
)
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [ERROR] Installing PyInstaller failed.
    goto :fail
)

REM -- 2. PyInstaller build --------------------------------------------------
echo.
echo [2/4] Building with PyInstaller...
python -m PyInstaller "%SPEC%" --noconfirm --clean --distpath dist --workpath build
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    goto :fail
)
if not exist "dist\ISOForgeZR\%APP_EXE%" (
    echo [ERROR] dist\ISOForgeZR\%APP_EXE% was not produced.
    goto :fail
)

REM -- 3. Version from app\__init__.py ---------------------------------------
set "APP_VERSION="
for /f "tokens=2 delims==" %%V in ('findstr /B /C:"__version__" "app\__init__.py"') do set "APP_VERSION=%%V"
if not defined APP_VERSION (
    echo [ERROR] Could not read __version__ from app\__init__.py
    goto :fail
)
set "APP_VERSION=%APP_VERSION: =%"
set "APP_VERSION=%APP_VERSION:"=%"
echo.
echo [3/4] Version: %APP_VERSION%

REM -- 4. Inno Setup ---------------------------------------------------------
set "ISCC="
for %%I in (ISCC.exe) do if not "%%~$PATH:I"=="" set "ISCC=%%~$PATH:I"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [ERROR] Inno Setup 6 ^(ISCC.exe^) not found.
    echo         Install it from https://jrsoftware.org/isdl.php
    echo         or run:  winget install JRSoftware.InnoSetup
    echo         The PyInstaller build in dist\ISOForgeZR is complete.
    goto :fail
)

echo.
echo [4/4] Compiling installer with "%ISCC%"...
"%ISCC%" /Q /DMyAppVersion=%APP_VERSION% "%ISS%"
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    goto :fail
)

set "SETUP_EXE=installer\Output\ISOForgeZR-Setup-%APP_VERSION%.exe"
if not exist "%SETUP_EXE%" (
    echo [ERROR] Expected %SETUP_EXE% but it was not created.
    goto :fail
)

echo.
echo ===== SUCCESS =====
echo Installer: %CD%\%SETUP_EXE%
echo.
start "" explorer /select,"%CD%\%SETUP_EXE%"
goto :done

:fail
echo.
echo ===== BUILD FAILED =====
if /I not "%~1"=="nopause" pause
endlocal
exit /b 1

:done
if /I not "%~1"=="nopause" pause
endlocal
exit /b 0
