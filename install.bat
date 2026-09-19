@echo off
setlocal EnableDelayedExpansion
title Granny Legacy Seed Predictor - Installer
cd /d "%~dp0"

REM ===================================================================
REM  One-click installer. Everything is automatic - the only thing the
REM  user ever has to do is press ENTER once at the start.
REM
REM   Step 1  find Python, or install it from python.org
REM   Step 2  install the libraries the program needs
REM   Step 3  start the program (run.bat)
REM ===================================================================

set "PY_VER=3.12.10"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "PY="

cls
echo.
echo   ============================================================
echo      GRANNY LEGACY SEED PREDICTOR  -  INSTALLER
echo   ============================================================
echo.
echo    This will set everything up for you and then start the
echo    program. It takes a few minutes.
echo.
echo    You do not have to do anything else. If a window pops up
echo    asking for permission, click Yes.
echo.
echo   ------------------------------------------------------------
echo.
pause
echo.

REM ---------------------------------------------------------------- 1
echo   STEP 1 of 3   Checking for Python...
echo.

call :find_python
if defined PY goto :got_python

echo    Python is not installed on this computer yet.
echo    Downloading it from python.org (about 25 MB)...
echo.

set "PY_SETUP=%TEMP%\python-%PY_VER%-amd64.exe"
curl.exe -L --fail -# -o "%PY_SETUP%" "%PY_URL%"
if not exist "%PY_SETUP%" powershell -NoProfile -Command "Invoke-WebRequest '%PY_URL%' -OutFile '%PY_SETUP%' -UseBasicParsing" >nul 2>&1
if not exist "%PY_SETUP%" goto :no_internet

echo.
echo    Installing Python. Please wait, this takes a few minutes
echo    and the screen may look frozen. Do not close this window.
echo.
"%PY_SETUP%" /passive InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1 Include_launcher=1
del "%PY_SETUP%" >nul 2>&1

call :find_python
if not defined PY goto :python_failed

:got_python
echo    OK - Python is ready.
echo.

REM ---------------------------------------------------------------- 2
echo   STEP 2 of 3   Installing the parts the program needs...
echo.

%PY% -m pip install --upgrade pip --disable-pip-version-check >nul 2>&1

%PY% -m pip install --disable-pip-version-check --quiet --no-warn-script-location "numpy>=1.24"
if errorlevel 1 goto :parts_failed
echo    OK - main parts installed.

REM Optional speed boost. If it fails, the program still works fine.
%PY% -m pip install --disable-pip-version-check --quiet --no-warn-script-location "pyopencl>=2023.1" >nul 2>&1
if errorlevel 1 (
    echo    Note: the graphics-card speed boost could not be installed.
    echo          That is fine - the program still works, just slower.
) else (
    echo    OK - graphics-card speed boost installed.
)
echo.

REM ---------------------------------------------------------------- 3
echo   STEP 3 of 3   Starting the program...
echo.
echo   ------------------------------------------------------------
echo      All done.  The program is opening now.
echo.
echo      Next time, you do not need this installer - just use
echo      run.bat to open the program.
echo   ------------------------------------------------------------
echo.
timeout /t 4 /nobreak >nul 2>&1
call "%~dp0run.bat"
exit /b 0


REM =================================================================
REM  Problem messages - each one says exactly what to do next.
REM =================================================================

:no_internet
echo.
echo   ------------------------------------------------------------
echo      COULD NOT DOWNLOAD PYTHON
echo.
echo      Please check that this computer is connected to the
echo      internet, then run install.bat again.
echo   ------------------------------------------------------------
echo.
pause
exit /b 1

:python_failed
echo.
echo   ------------------------------------------------------------
echo      ALMOST THERE - ONE MORE STEP
echo.
echo      Python was installed, but this window cannot see it yet.
echo.
echo      Please close this window and double-click install.bat
echo      one more time. It will finish the job.
echo   ------------------------------------------------------------
echo.
pause
exit /b 1

:parts_failed
echo.
echo   ------------------------------------------------------------
echo      SOMETHING WENT WRONG
echo.
echo      The program parts could not be installed. This is almost
echo      always a lost internet connection or an antivirus
echo      blocking it.
echo.
echo      Check your internet, then run install.bat again.
echo   ------------------------------------------------------------
echo.
pause
exit /b 1


REM =================================================================
REM  :find_python - looks everywhere Python is normally installed and
REM  sets PY to the first one that is version 3.9 or newer.
REM =================================================================
:find_python
call :test_python "py" "-3"
if not defined PY call :test_python "python" ""
for %%V in (313 312 311 310 39) do (
    if not defined PY call :test_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" ""
    if not defined PY call :test_python "%ProgramFiles%\Python%%V\python.exe" ""
)
exit /b 0

:test_python
set CAND="%~1" %~2
%CAND% -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%CAND%"
exit /b 0
