"""FastAPI application entry point for the Multi-Stream Listener & Transcriber.

Defines the lifespan hooks for startup/shutdown, all REST API endpoints,
static file serving with SPA catch-all, and error handling.

Requirements: 1.1–1.7, 2.1–2.5, 3.1–3.4, 4.1–4.2, 5.1–5.4, 6.1–6.5,
              7.1, 12.1–12.5, 14.1–14.6, 15.1–15.4
"""

from __future__ import annotations

import atexit
import logging
import platform
import signal
import subprocess as _subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config_manager import ConfigManager
from models import (
    AddStreamRequest,
    StartAllResponse,
    StreamStatusResponse,
    UpdateStreamRequest,
)
from stream_manager import ConcurrencyLimitError, StreamManager
from transcriber import Transcriber


# ---------------------------------------------------------------------------
# Logging configuration — logs to both console and file
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).parent / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
    ],
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_to_response(status) -> dict:
    """Convert a StreamStatus dataclass to a serialisable dict."""
    return StreamStatusResponse(
        id=status.id,
        brand=status.brand,
        url=status.url,
        state=status.state.value if hasattr(status.state, "value") else str(status.state),
        error_message=status.error_message,
        reconnect_attempt=status.reconnect_attempt,
        has_transcript=status.has_transcript,
        extractor_type=status.extractor_type,
        daily_file_path=status.daily_file_path,
    ).model_dump()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise ConfigManager, load Transcriber, create StreamManager.

    Shutdown: clean up all stream workers, subprocesses, and session files.

    Registers belt-and-suspenders cleanup via three mechanisms (Req 14.3):
      1. FastAPI lifespan shutdown (primary – fires on graceful shutdown)
      2. ``atexit`` handler (backup – fires on interpreter exit)
      3. SIGTERM / SIGINT signal handlers (safety net – fires on kill/Ctrl+C)

    ``StreamManager.cleanup()`` is idempotent, so multiple invocations
    from overlapping shutdown paths are harmless.
    """
    config_manager = ConfigManager()
    transcriber = Transcriber(config_manager.config.whisper_model)
    transcriber.load_model()
    stream_manager = StreamManager(config_manager, transcriber)

    app.state.config_manager = config_manager
    app.state.transcriber = transcriber
    app.state.stream_manager = stream_manager

    # --- Cleanup registrations (Req 14.3) ---------------------------------

    # Backup: atexit fires when the interpreter is shutting down.
    atexit.register(stream_manager.cleanup)

    # Safety net: signal handlers ensure cleanup even if lifespan shutdown
    # is bypassed.  Handlers are wrapped in try/except so a failure inside
    # cleanup never prevents the process from exiting.  After cleanup the
    # default signal disposition is restored and the signal re-raised so
    # the OS sees a normal signal exit.
    def _signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — initiating process cleanup …", sig_name)
        try:
            stream_manager.cleanup()
        except Exception:
            logger.exception("Error during signal-driven cleanup")
        # Restore the default handler and re-raise so the process exits
        # with the correct signal status (important for parent supervisors).
        signal.signal(signum, signal.SIG_DFL)
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _signal_handler)
    # SIGTERM is not available on Windows
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, _signal_handler)

    logger.info(
        "Application started — model '%s' loaded.",
        config_manager.config.whisper_model,
    )

    yield

    # Primary shutdown path (Req 14.1): terminate all streamlink/ffmpeg
    # child processes, close session files, delete temp .wav chunks.
    logger.info("Lifespan shutdown: running process cleanup …")
    stream_manager.cleanup()
    logger.info("Application shutdown complete.")


app = FastAPI(title="Multi-Stream Listener & Transcriber", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Validation errors → 400."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
    """Not-found errors → 404."""
    detail = exc.args[0] if exc.args else "Not found"
    return JSONResponse(status_code=404, content={"detail": str(detail)})


@app.exception_handler(ConcurrencyLimitError)
async def concurrency_error_handler(
    request: Request, exc: ConcurrencyLimitError
) -> JSONResponse:
    """Concurrency limit reached → 409."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# API endpoints — streams
# ---------------------------------------------------------------------------


@app.get("/api/streams")
async def list_streams(request: Request):
    """Return all stream statuses and the configured concurrency limit."""
    sm: StreamManager = request.app.state.stream_manager
    cm: ConfigManager = request.app.state.config_manager
    statuses = sm.get_all_statuses()
    return {
        "streams": [_status_to_response(s) for s in statuses],
        "max_concurrent_streams": cm.config.max_concurrent_streams,
    }


@app.post("/api/streams", status_code=201)
async def add_stream(body: AddStreamRequest, request: Request):
    """Add a new stream source and return its initial status."""
    cm: ConfigManager = request.app.state.config_manager
    sm: StreamManager = request.app.state.stream_manager

    new_source = cm.add_stream(body.brand, body.url)

    # Return the newly created stream's status from StreamManager
    for s in sm.get_all_statuses():
        if s.id == new_source.id:
            return _status_to_response(s)

    # Fallback — stream just created, not yet tracked by manager
    return StreamStatusResponse(
        id=new_source.id,
        brand=new_source.brand,
        url=new_source.url,
        state="idle",
    ).model_dump()


@app.put("/api/streams/{stream_id}")
async def update_stream(
    stream_id: str, body: UpdateStreamRequest, request: Request
):
    """Update an existing stream source. Blocked while the stream is listening."""
    cm: ConfigManager = request.app.state.config_manager
    sm: StreamManager = request.app.state.stream_manager

    if sm.is_stream_listening(stream_id):
        return JSONResponse(
            status_code=409,
            content={"detail": "Stream must be paused before editing"},
        )

    updated = cm.update_stream(stream_id, brand=body.brand, url=body.url)

    for s in sm.get_all_statuses():
        if s.id == updated.id:
            return _status_to_response(s)

    return StreamStatusResponse(
        id=updated.id,
        brand=updated.brand,
        url=updated.url,
        state="idle",
    ).model_dump()


@app.delete("/api/streams/{stream_id}")
async def remove_stream(stream_id: str, request: Request):
    """Remove a stream source. Blocked while the stream is listening."""
    cm: ConfigManager = request.app.state.config_manager
    sm: StreamManager = request.app.state.stream_manager

    if sm.is_stream_listening(stream_id):
        return JSONResponse(
            status_code=409,
            content={"detail": "Stream must be paused before removal"},
        )

    cm.remove_stream(stream_id)
    return JSONResponse(status_code=200, content={"detail": "Stream removed"})


# ---------------------------------------------------------------------------
# API endpoints — stream control
# ---------------------------------------------------------------------------

# Fixed-path routes defined before parameterised routes to avoid
# FastAPI treating "start-all" / "stop-all" as a {stream_id} value.


@app.post("/api/streams/start-all")
async def start_all_streams(request: Request):
    """Start all idle streams up to the concurrency limit."""
    sm: StreamManager = request.app.state.stream_manager
    result = sm.start_all()
    return StartAllResponse(
        started=result["started"],
        failed=result["failed"],
        skipped=result["skipped"],
    ).model_dump()


@app.post("/api/streams/stop-all")
async def stop_all_streams(request: Request):
    """Stop all listening streams."""
    sm: StreamManager = request.app.state.stream_manager
    statuses = sm.stop_all()
    return {"streams": [_status_to_response(s) for s in statuses]}


@app.post("/api/streams/{stream_id}/start")
async def start_stream(stream_id: str, request: Request):
    """Start listening on a single stream."""
    sm: StreamManager = request.app.state.stream_manager
    try:
        status = sm.start_stream(stream_id)
        return _status_to_response(status)
    except RuntimeError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.post("/api/streams/{stream_id}/stop")
async def stop_stream(stream_id: str, request: Request):
    """Stop listening on a single stream."""
    sm: StreamManager = request.app.state.stream_manager
    status = sm.stop_stream(stream_id)
    return _status_to_response(status)


# ---------------------------------------------------------------------------
# API endpoints — download
# ---------------------------------------------------------------------------


@app.get("/api/streams/{stream_id}/download")
async def download_transcript(stream_id: str, request: Request):
    """Download the session transcript file for a stream."""
    sm: StreamManager = request.app.state.stream_manager
    path = sm.get_download_path(stream_id)

    if path is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "No transcript available for this stream"},
        )

    file_path = Path(path)
    if not file_path.is_file():
        return JSONResponse(
            status_code=404,
            content={"detail": "Transcript file not found"},
        )

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="text/plain",
    )


@app.post("/api/open-folder")
async def open_folder(request: Request):
    """Open a file's containing folder in the OS file explorer."""
    body = await request.json()
    file_path = body.get("path", "")
    
    if not file_path:
        return JSONResponse(status_code=400, content={"detail": "No path provided"})
    
    # Get the directory containing the file
    from pathlib import Path as _Path
    target = _Path(file_path)
    folder = target.parent if target.is_file() else target
    
    if not folder.is_dir():
        return JSONResponse(status_code=404, content={"detail": "Directory not found"})
    
    # Open in OS file explorer
    system = platform.system()
    try:
        if system == "Darwin":  # macOS
            _subprocess.Popen(["open", str(folder)])
        elif system == "Windows":
            _subprocess.Popen(["explorer", str(folder)])
        else:  # Linux
            _subprocess.Popen(["xdg-open", str(folder)])
        return {"detail": "Opened", "path": str(folder)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

# ---------------------------------------------------------------------------
# Static file serving & SPA catch-all
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_root():
    """Serve the Dashboard entry-point HTML."""
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(str(index), media_type="text/html")
    return JSONResponse(
        status_code=404, content={"detail": "Dashboard not found"}
    )


# Mount static files at /static so CSS, JS, etc. are served with correct
# MIME types.  This must come AFTER all API routes but handles /static/*
# requests before the SPA catch-all below.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
