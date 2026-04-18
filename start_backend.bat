@echo off
setlocal EnableExtensions

cd /d "%~dp0backend"
if not exist "main.py" (
    echo Cannot find backend\main.py.
    echo Please keep this file in the m5paper-main folder.
    pause
    exit /b 1
)

echo.
echo === M5Paper backend ===
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo Port 8090 is already in use. The backend may already be running.
    echo.
    echo Try:
    echo   http://127.0.0.1:8090/admin
    echo   http://127.0.0.1:8090/dashboard
    echo.
    pause
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
    if errorlevel 1 goto error
)

echo Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo Local admin:
echo   http://127.0.0.1:8090/admin
echo.
echo Local dashboard:
echo   http://127.0.0.1:8090/dashboard
echo.
echo Network dashboard URLs to try from your phone or M5Paper:
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ips = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' }; foreach ($ip in $ips) { '  http://' + $ip.IPAddress + ':8090/dashboard  (' + $ip.InterfaceAlias + ')' }"
echo.
echo Keep this window open. Press Ctrl+C to stop the backend.
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8090
goto end

:error
echo.
echo Backend startup failed. Keep this window open and send the error text to Codex.
pause

:end
endlocal
