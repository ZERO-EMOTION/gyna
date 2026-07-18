@echo off
rem PARALLAX — Gyna multi-instance launcher with auto-restart.
rem   START GYNA.bat            -> starts every instance under instances\
rem   START GYNA.bat XAUUSD     -> starts one instance
rem One shared codebase; each instance runs in its OWN folder (its .env,
rem databases, logs) — no code copying. Crash -> relaunch after 10s.

if "%~1"=="run" goto run

if not "%~1"=="" (
    call :launch %~1
    exit /b
)
for /d %%I in ("%~dp0instances\*") do call :launch %%~nxI
exit /b

:launch
if not exist "%~dp0instances\%~1\.env" (
    echo [GYNA] Skipping %~1 — no .env in instances\%~1 ^(copy .env.example^)
    exit /b
)
start "GYNA-%~1" cmd /c ""%~f0" run %~1"
exit /b

:run
set SYM=%~2
title GYNA-%SYM%
cd /d "%~dp0instances\%SYM%"
:loop
py -3 "%~dp0main.py"
echo.
echo [GYNA-%SYM%] Process exited — restarting in 10 seconds (close window to stop)...
timeout /t 10 /nobreak >nul
goto loop
