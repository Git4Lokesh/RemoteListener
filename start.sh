#!/bin/bash

# Multi-Stream Listener & Transcriber — macOS/Linux launcher

PORT=${1:-8080}

# cd to the directory where this script lives
cd "$(dirname "$0")"

# Activate virtual environment if present
if [ -d "venv" ]; then
    echo "Activating virtual environment (venv)..."
    source venv/bin/activate
elif [ -d ".venv" ]; then
    echo "Activating virtual environment (.venv)..."
    source .venv/bin/activate
fi

# Check that required external tools are installed
if ! command -v ffmpeg &> /dev/null; then
    echo "Error: ffmpeg is not installed or not on PATH."
    echo "Install it with: brew install ffmpeg (macOS) or sudo apt install ffmpeg (Linux)"
    exit 1
fi

if ! command -v streamlink &> /dev/null; then
    echo "Error: streamlink is not installed or not on PATH."
    echo "Install it with: pip install streamlink"
    exit 1
fi

echo "Starting Multi-Stream Listener & Transcriber..."
echo "Dashboard will be available at http://localhost:$PORT"

# Start uvicorn in the background, then open the browser after a short delay
python -m uvicorn main:app --host 0.0.0.0 --port "$PORT" &
UVICORN_PID=$!

# Give the server a moment to start, then open the dashboard
sleep 2
if [ "$(uname)" = "Darwin" ]; then
    open "http://localhost:$PORT"
else
    xdg-open "http://localhost:$PORT" 2>/dev/null || echo "Open http://localhost:$PORT in your browser."
fi

# Bring uvicorn back to the foreground so Ctrl+C stops it cleanly
wait $UVICORN_PID
