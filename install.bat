@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Dependencies installed. Run start.bat to launch SlotBot.
pause
exit /b 0
:failed
echo Installation failed. Install Python 3.11 or 3.12 with Tkinter, then try again.
pause
exit /b 1
