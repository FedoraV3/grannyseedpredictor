@echo off
REM ===================================================================
REM  Granny Legacy Seed Predictor - debug launcher
REM  Runs the GUI with a visible console so Python errors are readable.
REM  Use this if run.bat appears to do nothing.
REM ===================================================================

cd /d "%~dp0"

echo Starting Seed Predictor (debug mode)...
echo Working directory: %CD%
echo.

python gui.py
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo  ---------------------------------------------------------
    echo   Exited with code %RC%. The error above is the cause.
    echo  ---------------------------------------------------------
) else (
    echo  Closed normally.
)
echo.
pause
