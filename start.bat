@echo off
echo Starting Shopfloor Control...
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python from https://python.org
    pause
    exit /b 1
)

:: Install Flask if needed
echo Installing dependencies...
pip install flask -q

:: Start the app and open browser
echo.
echo Starting server at http://127.0.0.1:5000
echo Press Ctrl+C to stop the server.
echo.
start "" http://127.0.0.1:5000
python app.py
pause
