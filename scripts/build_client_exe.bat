@echo off
REM VM Alignment Client — Build Installer EXE
REM Run from the project root:
REM   scripts\build_client_exe.bat
REM
REM Requirements:
REM   pip install pyinstaller pika requests pywin32

echo =====================================================
echo  VM Alignment Client — EXE Builder
echo =====================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    pause
    exit /b 1
)

REM Check pyinstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
)

REM Check key dependencies
python -c "import pika" >nul 2>&1
if errorlevel 1 (
    echo Installing pika...
    pip install pika
)

python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Installing requests...
    pip install requests
)

REM Go to client directory
cd /d "%~dp0..\client"

echo.
echo Building EXE (this may take 1-2 minutes)...
echo.

REM Clean old builds
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build with PyInstaller
python -m PyInstaller VMAlignmentClient.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo BUILD FAILED. Check the error messages above.
    pause
    exit /b 1
)

echo.
echo =====================================================
echo  Build complete!
echo =====================================================
echo.
echo EXE location:
dir /b /s dist\*.exe 2>nul
echo.
echo The EXE is self-contained and can be distributed
echo to any Windows machine. Run it to set up the client.
echo.
pause
