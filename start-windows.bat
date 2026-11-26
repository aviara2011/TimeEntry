@echo off
rem Double-click to start TimeEntry on Windows. Run "start-windows.bat demo" for the demo company.
cd /d "%~dp0"
where py >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and run this again. & pause & exit /b 1)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
if "%1"=="demo" (python -m timeentry --demo) else (python -m timeentry)
