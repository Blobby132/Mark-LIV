@echo off
REM ===========================================================================
REM  Check whether JARVIS can see Minecraft
REM
REM  Exists because `py tools\bridge_check.py` only works from the project
REM  folder, and "cd to the right directory first" is a step that reads as
REM  obvious in instructions and is not -- it failed twice in as many days,
REM  both times with an error about a path in the user's home directory.
REM
REM  `%~dp0` is this file's own folder, so double-clicking works from
REM  anywhere, and so does running it with a full path.
REM ===========================================================================

setlocal
cd /d "%~dp0"
title Check whether JARVIS can see Minecraft

set "PY="
for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY py -c "import sys" >nul 2>&1 && set "PY=py"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"

if not defined PY (
    echo.
    echo  [FAIL] No Python interpreter was found.
    echo  Install Python 3.11 - 3.14 from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH" on the first installer screen.
    echo.
    pause
    exit /b 9009
)

%PY% "tools\bridge_check.py" %*
set "RC=%ERRORLEVEL%"

REM The Python side pauses on Windows already; this catches the case where it
REM died before reaching that, which is the one nobody gets to read.
if not "%RC%"=="0" (
    if not "%RC%"=="1" (
        echo.
        echo  Exited with code %RC%.
        pause
    )
)

endlocal & exit /b %RC%
