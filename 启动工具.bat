@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "volume_tool.py" --gui
    exit /b
)

python "volume_tool.py" --gui
if errorlevel 1 pause
