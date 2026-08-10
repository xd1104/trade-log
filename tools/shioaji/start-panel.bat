@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Live Win-Rate Panel - TXF
echo.
echo   Starting live panel...
echo   Browser will open at http://127.0.0.1:8770/
echo   (Ctrl+C to stop)
echo.
"..\..\.venv\Scripts\python.exe" live_panel.py %*
echo.
echo   Stopped. Press any key to close.
pause >nul
endlocal
