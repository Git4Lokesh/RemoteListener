#!/bin/bash

# Multi-Stream Listener & Transcriber — macOS/Linux launcher
# Just run this script to start everything.

cd "$(dirname "$0")"

PORT=${1:-8080}

echo "============================================"
echo "  Multi-Stream Listener and Transcriber"
echo "============================================"
echo ""

# --- Check Python ---
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 is not installed or not on PATH."
    echo "        Python 3.10+ is required. Download from https://python.org"
    exit 1
fi

# --- Check ffmpeg ---
if ! command -v ffmpeg &> /dev/null; then
    echo "[ERROR] ffmpeg is not installed or not on PATH."
    echo "        Install with: brew install ffmpeg (macOS) or sudo apt install ffmpeg (Linux)"
    exit 1
fi

# --- Create virtual environment on first run ---
if [ ! -d "venv" ]; then
    echo "Setting up for first time..."
    python3 -m venv venv
    echo ""
fi

# --- Activate virtual environment ---
source venv/bin/activate

# --- Install dependencies if needed ---
if ! python -c "import faster_whisper" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    playwright install chromium
    echo ""
fi

echo "Starting server..."
echo "Dashboard will be available at http://localhost:$PORT"
echo ""

# --- Start uvicorn in background, open browser, then wait ---
python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" &
UVICORN_PID=$!

sleep 2

if [ "$(uname)" = "Darwin" ]; then
    open "http://localhost:$PORT"
else
    xdg-open "http://localhost:$PORT" 2>/dev/null || echo "Open http://localhost:$PORT in your browser."
fi

wait $UVICORN_PID
