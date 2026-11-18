@echo off
cd /d "%~dp0"
if exist "%~dp0LiquidGlassForWindows.exe" (
    start "" "%~dp0LiquidGlassForWindows.exe" --settings
) else (
    start "" python "%~dp0main.py" --settings
)
exit
