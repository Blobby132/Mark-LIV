@echo off
cd /d "%~dp0"
py tools\minecraft_gameplay_check.py %*
echo.
pause
