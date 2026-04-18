@echo off
setlocal EnableExtensions

cd /d "%~dp0backend"

echo.
echo === Stop M5Paper backend ===
echo.

if exist "backend.pid" (
    for /f "usebackq delims=" %%p in ("backend.pid") do (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$pidValue = %%p; $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue; if ($proc) { Stop-Process -Id $pidValue -Force; exit 0 } else { exit 1 }"
        if not errorlevel 1 (
            del "backend.pid" >nul 2>nul
            echo Stopped backend process %%p.
            pause
            exit /b 0
        )
    )
)

echo No saved backend process was found. Checking port 8090...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$conns = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue; if (-not $conns) { exit 1 }; $ids = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($id in $ids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }; exit 0"
if not errorlevel 1 (
    del "backend.pid" >nul 2>nul
    echo Stopped process listening on port 8090.
) else (
    echo Backend is not running on port 8090.
)

pause
endlocal
