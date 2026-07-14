@echo off
rem PARALLAX — Gyna 1-click stop. Kills the GYNA window and its Python child.
taskkill /FI "WINDOWTITLE eq GYNA*" /T /F
echo Gyna stopped. Open positions remain protected by the emergency broker SL.
pause
