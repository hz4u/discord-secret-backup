@echo off
rem Builds dist\Secret.exe (single file, no console window).
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo [1/4] Creating virtual environment...
    python -m venv .venv || goto :error
)

echo [2/4] Installing dependencies...
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :error

echo [3/4] Running tests...
.venv\Scripts\python.exe -m pytest -q || goto :error

echo [4/4] Building Secret.exe...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name Secret ^
    --icon assets\secret.ico ^
    --version-file version_info.txt ^
    --add-data "assets\fonts;assets\fonts" ^
    --add-data "assets\icons;assets\icons" ^
    --add-data "assets\lang;assets\lang" ^
    --add-data "assets\secret.ico;assets" ^
    --collect-data qtawesome ^
    --exclude-module tkinter ^
    --exclude-module customtkinter ^
    --exclude-module pytest ^
    run_secret.pyw || goto :error

echo.
echo Done: dist\Secret.exe
echo Copy it to the root of your BitLocker USB drive.
if not defined CI pause
exit /b 0

:error
echo.
echo Build failed.
if not defined CI pause
exit /b 1
