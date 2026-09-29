@echo off
REM ===================================================================
REM  Builds dumps\config_*.json from the game files (no mod needed).
REM  Double-click to find the game in Steam, or drag the game folder
REM  onto this file.
REM ===================================================================
cd /d "%~dp0"
python -c "import UnityPy" >nul 2>&1 || python -m pip install --disable-pip-version-check --quiet UnityPy
python make_dump.py %*
echo.
pause
