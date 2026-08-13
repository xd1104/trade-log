@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Morning Panel - TXF

rem Watchdog loop. The Shioaji SDK can take the whole process down when a
rem connection drops (seen 2026-08-12 19:17, no Python traceback), so bring
rem it back automatically instead of leaving Benson to notice it died.
rem
rem Sleep uses ping, not timeout: timeout needs a console input handle and
rem fails with "Input redirection is not supported" when the tool-manager
rem spawns this without a TTY.
rem
rem Exit code 2 = a panel is already running on the port. Stop, do not loop.

:loop
echo.
echo   [%date% %time%] starting panel...
echo   http://127.0.0.1:8770/
echo.
"..\..\.venv\Scripts\python.exe" live_panel.py %*
if errorlevel 2 goto already
echo.
echo   [%date% %time%] panel exited. restarting in 10s...
ping -n 11 127.0.0.1 >nul
goto loop

:already
echo.
echo   Panel is already running - nothing to do.
ping -n 4 127.0.0.1 >nul
endlocal
