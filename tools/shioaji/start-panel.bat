@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Morning Panel - TXF

rem Auto-restart loop: the Shioaji SDK can take the whole process down on a
rem dropped connection (seen 2026-08-12 19:17, no Python traceback).
rem Never leave Benson to notice it died on his own - just bring it back.

:loop
echo.
echo   [%date% %time%] starting panel...
echo   http://127.0.0.1:8770/    (Ctrl+C twice to stop)
echo.
"..\..\.venv\Scripts\python.exe" live_panel.py %*
echo.
echo   [%date% %time%] panel exited. restarting in 10s...
echo   (press Ctrl+C now to stop for good)
timeout /t 10 >nul
goto loop
