@echo off
rem PARALLAX — Gyna 1-click launcher with auto-restart.
rem Double-click to start. The loop restarts Gyna 10s after any crash,
rem so the learning trader keeps itself running unattended.
if "%~1"=="run" goto run
start "GYNA" cmd /c ""%~f0" run"
exit /b

:run
title GYNA
cd /d "%~dp0"
:loop
python main.py
echo.
echo [GYNA] Process exited — restarting in 10 seconds (close window to stop)...
timeout /t 10 /nobreak >nul
goto loop
