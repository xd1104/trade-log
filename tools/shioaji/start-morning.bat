@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Morning Panel - TXF
echo.
echo   Morning Panel starting...
echo   (Ctrl+C to stop)
echo.
"..\..\.venv\Scripts\python.exe" morning_live.py %*
echo.
echo   Finished. Press any key to close.
pause >nul
endlocal
