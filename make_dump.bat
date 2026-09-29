@echo off
REM ===================================================================
REM  Builds dumps\config_*.json from the game files (no mod needed).
REM  Double-click and select the game's .exe (or level1), or drag the
REM  game folder onto this file.
REM ===================================================================
cd /d "%~dp0"
python -c "import UnityPy" >nul 2>&1 || python -m pip install --disable-pip-version-check --quiet UnityPy
python make_dump.py %*
echo.
pause
