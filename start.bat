@echo off

REM Multi-Stream Listener & Transcriber — Windows launcher

REM cd to the directory where this script lives
cd /d "%~dp0"

REM Activate virtual environment if present
if exist "venv\Scripts\activate.bat" (
    echo Activating virtual environment (venv)...
    call venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    echo Activating virtual environment (.venv)...
    call .venv\Scripts\activate.bat
)

REM Check that required external tools are installed
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo Error: ffmpeg is not installed or not on PATH.
    echo Install it from https://ffmpeg.org/download.html and add to PATH.
    pause
    exit /b 1
)

where streamlink >nul 2>nul
if %errorlevel% neq 0 (
    echo Error: streamlink is not installed or not on PATH.
    echo Install it with: pip install streamlink
    pause
    exit /b 1
)

echo Starting Multi-Stream Listener & Transcriber...
echo Dashboard will be available at http://localhost:8000

REM Open the dashboard in the default browser
start http://localhost:8000

REM Start uvicorn (this blocks until the server is stopped with Ctrl+C)
python -m uvicorn main:app --host 0.0.0.0 --port 8000
