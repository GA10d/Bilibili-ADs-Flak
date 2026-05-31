@echo off
cd /d "%~dp0.."
set PYTHONPATH=%cd%
echo Starting Bilibili ADs Flak GUI...
echo.
call conda run -n baf python src/gui/run.py
if %errorlevel% neq 0 (
    echo.
    echo GUI failed to start.
    pause
)
