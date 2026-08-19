@echo off
setlocal

set "HERE=%~dp0"
cd /d "%HERE%"

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [FAIL] pyinstaller not found.
    pause
    exit /b 1
)

taskkill /IM SerialTool.exe /F >nul 2>nul

pyinstaller --noconfirm --clean --onefile --windowed ^
    --name SerialTool ^
    --icon "%HERE%icon.ico" ^
    --add-data "%HERE%icon.ico;." ^
    --distpath "%HERE%..\exe" ^
    --workpath "%HERE%..\build" ^
    --specpath "%HERE%..\build" ^
    "%HERE%serial_tool.py"

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" if exist "%HERE%..\exe\SerialTool.exe" (
    echo [OK] Built: %HERE%..\exe\SerialTool.exe
) else (
    echo [FAIL] Build failed.
    exit /b %RC%
)
pause
