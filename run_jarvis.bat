@echo off
REM ===========================================================================
REM  MARK LIV -- Windows launcher
REM
REM  Double-clicking main.py opens a console, runs Python, and closes the
REM  console the moment the process exits. If Python exits because of an
REM  uncaught exception, the traceback is printed into a window that has
REM  already closed -- all the user sees is a flash.
REM
REM  This launcher exists so that never happens. It runs the app from the
REM  right directory with a known-good interpreter, and if the app exits with
REM  an error it runs the doctor and holds the window open so the reason can
REM  actually be read.
REM ===========================================================================

setlocal
cd /d "%~dp0"

title MARK LIV

REM --- Find an interpreter -------------------------------------------------
REM  The py launcher is installed by python.org and knows about every Python
REM  on the machine, so it is tried first and asked for a supported version
REM  explicitly. Falling back to bare "python" covers Store installs and PATH
REM  setups where py is absent.
set "PY="
for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    py -c "import sys" >nul 2>&1 && set "PY=py"
)
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo.
    echo  [FAIL] No Python interpreter was found.
    echo.
    echo  Install Python 3.11 - 3.14 from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH" on the first installer screen.
    echo.
    pause
    exit /b 9009
)

echo  Using interpreter: %PY%
echo.

REM --- First run? Dependencies may not be installed yet ---------------------
%PY% -c "import PyQt6, sounddevice, numpy, google.genai" >nul 2>&1
if errorlevel 1 (
    echo  Dependencies are not installed yet. Running setup first.
    echo  This downloads about 1 GB and takes roughly ten minutes.
    echo.
    %PY% setup.py
    if errorlevel 1 (
        echo.
        echo  [FAIL] Setup did not finish. The output above says why.
        echo.
        pause
        exit /b 1
    )
    echo.
)

REM --- Launch ---------------------------------------------------------------
%PY% main.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo ========================================================
    echo   MARK LIV exited with error code %RC%.
    echo   Running the doctor to work out why...
    echo ========================================================
    echo.
    %PY% tools\doctor.py --no-pause
    echo.
    echo  Scroll up to read the error, then send it to me.
    echo.
    pause
)

REM  `endlocal & exit` in one statement: the %RC% is expanded before
REM  endlocal discards it, which a separate `exit /b %RC%` line would not.
endlocal & exit /b %RC%
