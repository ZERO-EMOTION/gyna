@echo off
rem PARALLAX — Gyna 1-click stop.
rem   STOP GYNA.bat            -> stops every instance
rem   STOP GYNA.bat XAUUSD     -> stops one instance
if "%~1"=="" (
    taskkill /FI "WINDOWTITLE eq GYNA-*" /T /F
) else (
    taskkill /FI "WINDOWTITLE eq GYNA-%~1*" /T /F
)
echo Gyna stopped. Open positions remain protected by the emergency broker SL.
pause
