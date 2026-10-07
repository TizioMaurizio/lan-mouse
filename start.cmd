@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 bootstrap.py
) else (
    python bootstrap.py
)
if errorlevel 1 (
    echo.
    echo Install Python 3.11 or newer from python.org if Python is missing.
    echo Enable "Add python.exe to PATH" during installation.
    pause
)

