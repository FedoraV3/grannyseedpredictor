@echo off
setlocal EnableDelayedExpansion
REM ===================================================================
REM  Granny Legacy Seed Predictor - debug launcher
REM  Runs the updater and GUI with a visible console so Python errors
REM  are readable. Use this if run.bat appears to do nothing.
REM ===================================================================

cd /d "%~dp0"

REM Everything below is one parenthesised block, so cmd.exe has read all of
REM it before Python starts: the auto-updater may rewrite this file.
(
    echo Starting Seed Predictor ^(debug mode^)...
    echo Working directory: %CD%
    echo.

    python updater.py
    set "RC=!errorlevel!"

    echo.
    if not "!RC!"=="0" (
        echo  ---------------------------------------------------------
        echo   Exited with code !RC!. The error above is the cause.
        echo  ---------------------------------------------------------
    ) else (
        echo  Closed normally.
    )
    echo.
    pause
    exit /b !RC!
)
