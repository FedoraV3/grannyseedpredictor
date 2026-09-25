@echo off
REM ===================================================================
REM  Granny Legacy Seed Predictor - launcher
REM  Updates and starts the GUI with pythonw.exe (no console window) and then
REM  closes this window immediately.
REM  If the GUI does not appear, run run_debug.bat instead - it keeps
REM  a console open and shows the error.
REM ===================================================================

cd /d "%~dp0"

set "PYW="

REM 1. pythonw.exe on PATH
where pythonw.exe >nul 2>&1 && set "PYW=pythonw.exe"

REM 2. the py launcher's windowed variant
if not defined PYW where pyw.exe >nul 2>&1 && set "PYW=pyw.exe"

REM 3. common per-user install locations
if not defined PYW (
    for %%V in (313 312 311 310 39) do (
        if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" (
            set "PYW=%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe"
        )
    )
)

if not defined PYW (
    echo.
    echo  ERROR: could not find pythonw.exe
    echo.
    echo  Install Python 3.9+ and make sure it is on your PATH,
    echo  or edit this file and set PYW to the full path of pythonw.exe
    echo.
    pause
    exit /b 1
)

REM updater.py checks GitHub for a new version, installs it, then opens
REM gui.py. "& exit" keeps this on one line: the update may rewrite this file.
start "" "%PYW%" "%~dp0updater.py" & exit
