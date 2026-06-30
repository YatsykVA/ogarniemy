@echo off
title CollectorHub

cd /d "%~dp0"

echo ==========================================
echo        CollectorHub Launcher
echo ==========================================
echo.

if not exist ".venv" (
    echo [1/5] Creating virtual environment...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo.
echo [2/5] Upgrading pip...
python -m pip install --upgrade pip

echo.
echo [3/5] Installing requirements...
pip install -r requirements.txt

echo.
echo [4/5] Installing Playwright Chromium...
python -m playwright install chromium

echo.
echo [5/5] Starting CollectorHub...
python main.py

echo.
echo CollectorHub has stopped.
pause
