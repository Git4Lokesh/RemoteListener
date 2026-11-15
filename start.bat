@echo off

REM Multi-Stream Listener & Transcriber — Windows launcher
REM Just double-click this file to start everything.

cd /d "%~dp0"

echo ============================================
echo   Multi-Stream Listener and Transcriber
echo ============================================
echo.

REM --- Check Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Python 3.10+ is required. Download from https://python.org
    echo.
    pause
    exit /b 1
)

REM --- Check ffmpeg ---
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] ffmpeg is not installed or not on PATH.
    echo         Install with: winget install Gyan.FFmpeg
    echo.
    pause
    exit /b 1
)

REM --- Create virtual environment on first run ---
if not exist "venv" (
    echo Setting up for first time...
    python -m venv venv
    echo.
)

REM --- Activate virtual environment ---
call venv\Scripts\activate.bat

REM --- Install dependencies if needed ---
python -c "import faster_whisper" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing dependencies...
    pip install -r requirements.txt
    playwright install chromium
    echo.
)

echo Starting server...
echo Dashboard will be available at http://localhost:8000
echo.

REM --- Open the dashboard in the default browser ---
start http://localhost:8000

REM --- Start uvicorn (blocks until Ctrl+C) ---
python -m uvicorn main:app --host 127.0.0.1 --port 8000
