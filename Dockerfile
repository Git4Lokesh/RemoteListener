FROM python:3.12-slim

# System deps: ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium + its OS deps
RUN playwright install chromium && playwright install-deps chromium

# App code
COPY main.py config_manager.py transcriber.py extractor.py \
     playwright_extractor.py stream_manager.py models.py ./
COPY static/ ./static/
COPY tests/ ./tests/

# Transcript mount point
RUN mkdir -p /transcripts

# Pre-download whisper model so first run is instant
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"

ENV PYTHONUNBUFFERED=1
ENV TRANSCRIPT_DIR=/transcripts
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
