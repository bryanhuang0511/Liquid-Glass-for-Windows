@echo off
cd /d "%~dp0"
if exist "%~dp0LiquidGlassForWindows.exe" (
    start "" "%~dp0LiquidGlassForWindows.exe"
) else (
    start "" pythonw "%~dp0main.py"
)
exit
