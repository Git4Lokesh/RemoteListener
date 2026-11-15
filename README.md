# Multi-Stream Listener and Transcriber

A 100% free, locally hosted web app that captures audio from live web broadcasts, transcribes in real time using [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and saves daily transcript files. No paid APIs, no cloud dependencies — everything runs on your machine.

---

## Prerequisites

- **Python 3.10+**
- **ffmpeg** installed and on your PATH

| Platform | Install command |
|----------|----------------|
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` (or download from [ffmpeg.org](https://ffmpeg.org/download.html)) — must be on PATH |
| Linux | `sudo apt install ffmpeg` |

## Installation

1. **Clone or download** the project:

   ```bash
   git clone <repo-url> RemoteListener
   cd RemoteListener
   ```

2. **Create a virtual environment:**

   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment:**

   ```bash
   # macOS / Linux
   source venv/bin/activate

   # Windows
   venv\Scripts\activate
   ```

4. **Install Python dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

5. **Install the Playwright browser:**

   ```bash
   playwright install chromium
   ```

## Usage

### Quick Start

**macOS / Linux:**

```bash
./start.sh
```

**Windows:**

```cmd
start.bat
```

The launcher script activates the virtual environment, checks that `ffmpeg` and `streamlink` are available, starts the server, and opens the dashboard in your default browser.

### Manual Start

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

## How It Works

1. **Add streams** — Use the dashboard to add a stream by entering a brand name and the URL.

2. **Press Play** — The app auto-detects the best extraction method:
   - **Streamlink** for YouTube Live, Twitch, DailyMotion, and other supported platforms (ad-free for YouTube).
   - **Playwright browser fallback** for any other website with a video/audio player. A headless Chromium instance loads the page, intercepts the media stream URL, and pipes audio through ffmpeg.

3. **Transcripts save automatically** — One file per day per brand at:

   ```
   ~/Desktop/transcripts/{BRAND}/{YYYY-MM-DD}/{BRAND}_{YYYY-MM-DD}.txt
   ```

   Lines are timestamped in real time:

   ```
   [14:32:05] Welcome back to the show everyone
   [14:32:15] Our first guest is joining us today
   ```

   Multiple listening sessions within the same day are appended to the same file, separated by session markers.

4. **Press Pause** — Stops capture. A clickable file path appears on the stream card so you can open the transcript folder directly.

5. **Auto-reconnect** — If a stream drops, the app retries up to 5 times with exponential backoff (2 s → 4 s → 8 s → 16 s → 32 s). Disconnect and reconnect events are logged in the transcript.

## Configuration

All settings live in `config.yaml` at the project root. The file is created with defaults on first launch if it doesn't exist. If any value is invalid, the app logs a warning and falls back to the default.

| Setting | Type | Range | Default |
|---------|------|-------|---------|
| `max_concurrent_streams` | integer | 1 – 20 | `5` |
| `chunk_duration_seconds` | integer | 5 – 300 | `10` |
| `whisper_model` | string | max 64 chars | `"base.en"` |
| `output_base_dir` | string | max 512 chars | `"~/Desktop/transcripts"` |
| `playwright_timeout_seconds` | integer | 5 – 60 | `15` |
| `playwright_auto_click_play` | boolean | `true` / `false` | `true` |
| `streams` | list | 0 – 50 entries | `[]` |

- **max_concurrent_streams** — How many streams can be actively listening at the same time.
- **chunk_duration_seconds** — Duration of each audio chunk (in seconds) sent to the transcriber.
- **whisper_model** — The faster-whisper model to load (e.g. `"base.en"`, `"small.en"`, `"tiny.en"`).
- **output_base_dir** — Root directory for transcript files.
- **playwright_timeout_seconds** — How long Playwright waits to discover a stream URL on a page.
- **playwright_auto_click_play** — Whether Playwright should attempt to click play buttons on the page automatically.
- **streams** — List of configured stream sources (managed through the dashboard UI). Each entry has `id`, `brand`, and `url`.

Example:

```yaml
max_concurrent_streams: 5
chunk_duration_seconds: 10
whisper_model: "base.en"
output_base_dir: "~/Desktop/transcripts"
playwright_timeout_seconds: 15
playwright_auto_click_play: true
streams:
  - id: "a1b2c3d4"
    brand: "CNBC"
    url: "https://www.cnbctv18.com/live-tv/"
```

## Supported Sources

| Source | Method |
|--------|--------|
| YouTube Live | Streamlink (ad-free) |
| Twitch | Streamlink |
| DailyMotion | Streamlink |
| Facebook Live | Streamlink |
| Any website with a video player | Playwright browser fallback |
| Direct HLS `.m3u8` URLs | ffmpeg direct |

Streamlink supports many more platforms — see the [full plugin list](https://streamlink.github.io/plugins.html).

## Project Structure

```
RemoteListener/
├── main.py                   # FastAPI app, API routes, lifespan hooks
├── stream_manager.py         # StreamManager / StreamWorker, transcript I/O
├── transcriber.py            # Transcriber wrapper for faster-whisper
├── config_manager.py         # ConfigManager — config.yaml read/write/validation
├── extractor.py              # AudioExtractor ABC, StreamlinkFFmpegExtractor, FallbackExtractor
├── playwright_extractor.py   # PlaywrightExtractor — headless browser audio capture
├── models.py                 # Shared data models (dataclasses, enums, Pydantic)
├── config.yaml               # Application configuration (auto-created on first run)
├── requirements.txt          # Python dependencies
├── start.sh                  # macOS / Linux launcher
├── start.bat                 # Windows launcher
├── static/
│   ├── index.html            # Dashboard UI
│   ├── app.js                # Dashboard client-side logic
│   └── style.css             # Dashboard styles
└── tests/
    ├── conftest.py            # Shared test fixtures
    ├── test_config_manager.py
    └── test_stream_helpers.py
```

## License

For personal use.
