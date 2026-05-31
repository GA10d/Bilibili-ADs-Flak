@echo off
cd /d "%~dp0.."
echo ========================================
echo    Bilibili Cookie Importer
echo ========================================
echo.
echo Reading Bilibili login info from browser...
echo (make sure you are logged in to bilibili.com)
echo.

set PYTHONPATH=%cd%
call conda run -n baf python -c "from src.cookie_importer import import_cookies, save_to_env; cookies = import_cookies(); save_to_env(cookies); print(); print('OK - SESSDATA: ' + cookies.sessdata[:20] + '...'); print('OK - bili_jct: ' + cookies.bili_jct[:20] + '...'); print(); print('Saved to .env')"

if %errorlevel% neq 0 (
    echo.
    echo ===== Import Failed =====
    echo Possible reasons:
    echo   1. Not logged in to bilibili.com in browser
    echo   2. Chrome/Edge is running (file locked) - close it first
    echo   3. No supported browser found (Chrome/Edge/Firefox)
    echo.
)

echo.
pause
