"""Stream lifecycle management for the Multi-Stream Listener & Transcriber.

Provides helper utilities for session file management, transcript formatting,
and reconnection backoff.  Contains the StreamWorker class that runs the
per-stream extract → transcribe → write loop, and the StreamManager class
that orchestrates stream lifecycles across threads.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Optional

from config_manager import ConfigManager
from extractor import AudioExtractor, FallbackExtractor, StreamlinkFFmpegExtractor
from models import StreamSource, StreamState, StreamStatus
from transcriber import Transcriber

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def generate_session_path(output_base_dir: str, brand: str, dt: datetime) -> str:
    """Build the full path for a new session transcript file.

    Path pattern::

        {output_base_dir}/{BRAND}/{YYYY-MM-DD}/{BRAND}_{YYYY-MM-DD}_{HH-MM-SS}.txt

    Missing parent directories are created automatically.  If a file with the
    generated name already exists, a numeric suffix ``_1``, ``_2``, … is
    appended to ensure uniqueness.

    Args:
        output_base_dir: Root directory for all transcript output.
        brand: Brand / channel name used in the directory and file name.
        dt: Timestamp (system local time) for date and time components.

    Returns:
        Absolute path to the new (unique) session file.
    """
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H-%M-%S")

    dir_path = os.path.join(output_base_dir, brand, date_str)
    os.makedirs(dir_path, exist_ok=True)

    base_name = f"{brand}_{date_str}_{time_str}"
    file_path = os.path.join(dir_path, f"{base_name}.txt")

    if not os.path.exists(file_path):
        return file_path

    # Handle collision by appending _1, _2, …
    counter = 1
    while True:
        file_path = os.path.join(dir_path, f"{base_name}_{counter}.txt")
        if not os.path.exists(file_path):
            return file_path
        counter += 1


def generate_daily_path(output_base_dir: str, brand: str, dt: datetime) -> str:
    """Build the path for a daily transcript file.

    Path pattern::

        {output_base_dir}/{BRAND}/{YYYY-MM-DD}/{BRAND}_{YYYY-MM-DD}.txt

    One file per brand per day.  All sessions for the day are appended
    to this file with session markers for demarcation.

    Missing parent directories are created automatically.
    """
    date_str = dt.strftime("%Y-%m-%d")
    dir_path = os.path.join(output_base_dir, brand, date_str)
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{brand}_{date_str}.txt")


def format_transcript_line(time: datetime, text: str) -> str:
    """Format a single transcript line with a wall-clock timestamp.

    Returns a string in the form ``[HH:MM:SS] {text}`` where hours, minutes,
    and seconds are zero-padded to two digits.

    Args:
        time: Wall-clock time (system local) for the transcript entry.
        text: Transcribed text to include after the timestamp.

    Returns:
        The formatted transcript line.
    """
    timestamp = time.strftime("%H:%M:%S")
    return f"[{timestamp}] {text}"


def compute_backoff_delay(attempt: int) -> int:
    """Compute the reconnection backoff delay for a given attempt number.

    Uses exponential backoff with a 2× multiplier capped at 32 seconds::

        attempt 1 → 2s, attempt 2 → 4s, attempt 3 → 8s,
        attempt 4 → 16s, attempt 5 → 32s

    Args:
        attempt: The 1-based retry attempt number.

    Returns:
        Delay in seconds before the next reconnection attempt.
    """
    return min(2 ** attempt, 32)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ConcurrencyLimitError(Exception):
    """Raised when the maximum concurrent streams limit has been reached."""


# ---------------------------------------------------------------------------
# Per-stream runtime context
# ---------------------------------------------------------------------------


@dataclass
class _StreamContext:
    """Mutable runtime state for a single stream, tracked by StreamManager.

    All fields are accessed under ``StreamManager._lock`` from both the main
    (API) thread and individual worker threads.
    """

    state: StreamState = StreamState.IDLE
    session_file_handle: Optional[IO[str]] = None
    session_file_path: Optional[str] = None
    worker_thread: Optional[threading.Thread] = None
    extractor: Optional[AudioExtractor] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    error_message: Optional[str] = None
    reconnect_attempt: Optional[int] = None


# ---------------------------------------------------------------------------
# StreamWorker
# ---------------------------------------------------------------------------


class StreamWorker:
    """Extract → transcribe → write loop for a single listening stream.

    Runs in a dedicated daemon thread spawned by :class:`StreamManager`.
    Handles automatic reconnection with exponential backoff on connection
    errors.

    Requirements: 4.1, 4.2, 4.7, 9.3, 10.3, 10.4, 10.6, 11.3,
                  13.1, 13.2, 13.3, 13.4, 13.5, 14.2, 14.4, 14.5, 14.6
    """

    MAX_RECONNECT_ATTEMPTS = 5

    def __init__(
        self,
        source: StreamSource,
        ctx: _StreamContext,
        transcriber: Transcriber,
        on_state_change,
        chunk_duration: int,
    ) -> None:
        """Initialise the worker with all dependencies.

        Args:
            source: The stream configuration (id, brand, url).
            ctx: Mutable runtime context shared with StreamManager.
            transcriber: Shared transcriber instance for audio → text.
            on_state_change: Callback to report state transitions.
            chunk_duration: Duration of each audio chunk in seconds.
        """
        self._source = source
        self._ctx = ctx
        self._transcriber = transcriber
        self._on_state_change = on_state_change
        self._chunk_duration = chunk_duration

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main worker entry point called from the daemon thread.

        Starts the extractor and enters the processing loop.  If the
        initial start fails with a ConnectionError the reconnection
        handler is invoked immediately.
        """
        try:
            self._ctx.extractor.start(self._source.url)
        except ConnectionError as exc:
            logger.error(
                "Initial extractor start failed for '%s': %s",
                self._source.brand, exc,
            )
            if not self._handle_reconnect():
                return

        self._processing_loop()

    # ------------------------------------------------------------------
    # Core processing loop
    # ------------------------------------------------------------------

    def _processing_loop(self) -> None:
        """Read chunks in background, transcribe, write to session file.

        Uses a background reader thread to continuously pull audio chunks
        from the extractor so the ffmpeg pipe never blocks during
        transcription.  This eliminates gaps in the transcript caused by
        the sequential read-then-transcribe approach.
        """
        import queue as _queue

        chunk_q: _queue.Queue = _queue.Queue(maxsize=10)
        reader_error: list = []  # ConnectionError from reader thread

        def _reader():
            """Background thread: continuously read chunks into the queue."""
            try:
                while not self._ctx.stop_event.is_set():
                    try:
                        chunk_path = self._ctx.extractor.read()
                    except ConnectionError as exc:
                        reader_error.append(exc)
                        chunk_q.put(None)  # signal the consumer
                        return
                    chunk_q.put(chunk_path)  # None = stream ended
                    if chunk_path is None:
                        return
            except Exception:
                chunk_q.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        while not self._ctx.stop_event.is_set():
            # --- Get next chunk (blocks until reader produces one) ---
            try:
                chunk_path = chunk_q.get(timeout=1.0)
            except _queue.Empty:
                continue

            # Reader hit a ConnectionError — attempt reconnect
            if reader_error:
                exc = reader_error[0]
                logger.warning(
                    "Connection lost for '%s': %s",
                    self._source.brand, exc,
                )
                if self._handle_reconnect():
                    # Restart reader for the new extractor
                    reader_error.clear()
                    while not chunk_q.empty():
                        old = chunk_q.get_nowait()
                        if old:
                            self._safe_delete(old)
                    reader_thread = threading.Thread(target=_reader, daemon=True)
                    reader_thread.start()
                    continue
                return

            # Stream ended gracefully
            if chunk_path is None:
                logger.info(
                    "Stream '%s' ended gracefully.", self._source.brand
                )
                break

            # Check stop signal before expensive transcription
            if self._ctx.stop_event.is_set():
                self._safe_delete(chunk_path)
                break

            # --- Transcribe the chunk ---
            text = self._transcriber.transcribe(chunk_path)

            # Delete the temp .wav immediately after transcription
            self._safe_delete(chunk_path)

            # Transition CONNECTING → LISTENING on first successful transcription
            if self._ctx.state == StreamState.CONNECTING:
                self._on_state_change(self._source.id, StreamState.LISTENING)
                logger.info("Stream '%s' now actively transcribing.", self._source.brand)

            # --- Append non-empty text to the session file ---
            if text and self._ctx.session_file_handle:
                line = format_transcript_line(datetime.now(), text)
                try:
                    self._ctx.session_file_handle.write(line + "\n")
                    self._ctx.session_file_handle.flush()
                except OSError as exc:
                    logger.error(
                        "Failed to write transcript for '%s': %s",
                        self._source.brand, exc,
                    )

        # Drain remaining queued chunks on exit
        while not chunk_q.empty():
            try:
                leftover = chunk_q.get_nowait()
                if leftover:
                    self._safe_delete(leftover)
            except _queue.Empty:
                break

    # ------------------------------------------------------------------
    # Reconnection with exponential backoff
    # ------------------------------------------------------------------

    def _handle_reconnect(self) -> bool:
        """Attempt reconnection up to 5 times with exponential backoff.

        Writes disconnect/reconnect markers to the session file and
        reports state transitions via the on_state_change callback.

        Returns:
            ``True`` if the stream was successfully reconnected,
            ``False`` if all attempts failed or a stop was requested.
        """
        disconnect_time = datetime.now()

        # Transition to RECONNECTING
        self._on_state_change(
            self._source.id,
            StreamState.RECONNECTING,
            reconnect_attempt=1,
        )

        # Write disconnect marker to session file
        self._write_marker(
            format_transcript_line(disconnect_time, "--- DISCONNECTED ---")
        )

        # Stop the broken extractor (kill processes, clean temp files)
        if self._ctx.extractor:
            try:
                self._ctx.extractor.stop()
            except Exception as exc:
                logger.warning(
                    "Error stopping extractor during reconnect for '%s': %s",
                    self._source.brand, exc,
                )

        # -- Retry loop --
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            if self._ctx.stop_event.is_set():
                return False

            # Update attempt number for UI display (Req 13.5)
            self._on_state_change(
                self._source.id,
                StreamState.RECONNECTING,
                reconnect_attempt=attempt,
            )

            delay = compute_backoff_delay(attempt)
            logger.info(
                "Reconnect attempt %d/%d for '%s' in %ds …",
                attempt, self.MAX_RECONNECT_ATTEMPTS,
                self._source.brand, delay,
            )

            # Sleep, but honour stop requests during the wait
            if self._ctx.stop_event.wait(timeout=delay):
                return False  # Stop requested during backoff

            try:
                # Create a fresh extractor and start it
                self._ctx.extractor = FallbackExtractor(
                    chunk_duration=self._chunk_duration,
                )
                self._ctx.extractor.start(self._source.url)

                # --- Reconnection successful ---
                reconnect_time = datetime.now()
                gap_seconds = int(
                    (reconnect_time - disconnect_time).total_seconds()
                )

                self._write_marker(
                    format_transcript_line(
                        reconnect_time,
                        f"--- RECONNECTED (gap: {gap_seconds}s) ---",
                    )
                )

                self._on_state_change(
                    self._source.id, StreamState.LISTENING,
                )

                logger.info(
                    "Reconnected '%s' after %ds gap.",
                    self._source.brand, gap_seconds,
                )
                return True

            except ConnectionError as exc:
                logger.warning(
                    "Reconnect attempt %d/%d failed for '%s': %s",
                    attempt, self.MAX_RECONNECT_ATTEMPTS,
                    self._source.brand, exc,
                )

        # -- All attempts exhausted --
        error_msg = (
            f"Stream disconnected after {self.MAX_RECONNECT_ATTEMPTS} "
            f"failed reconnection attempts"
        )

        self._on_state_change(
            self._source.id,
            StreamState.IDLE,
            error_message=error_msg,
        )

        # Close the session file (Req 13.4)
        if self._ctx.session_file_handle:
            try:
                self._ctx.session_file_handle.close()
            except OSError:
                pass
            self._ctx.session_file_handle = None

        logger.error(
            "All reconnect attempts exhausted for '%s'.", self._source.brand
        )
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_marker(self, text: str) -> None:
        """Write a marker line to the session file and flush to disk."""
        if self._ctx.session_file_handle:
            try:
                self._ctx.session_file_handle.write(text + "\n")
                self._ctx.session_file_handle.flush()
            except OSError as exc:
                logger.error(
                    "Failed to write marker to session file for '%s': %s",
                    self._source.brand, exc,
                )

    @staticmethod
    def _safe_delete(path: str) -> None:
        """Delete a file, logging on failure without raising."""
        try:
            os.unlink(path)
        except OSError as exc:
            logger.warning("Failed to delete temp file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# StreamManager
# ---------------------------------------------------------------------------


class StreamManager:
    """Manages the lifecycle of all configured streams.

    Responsibilities include starting / stopping individual and bulk streams,
    enforcing the concurrency limit, creating session files, spawning worker
    threads, tracking per-stream state in a thread-safe dictionary, and
    performing cleanup on application shutdown.
    """

    def __init__(
        self, config_manager: ConfigManager, transcriber: Transcriber
    ) -> None:
        self._config_manager = config_manager
        self._transcriber = transcriber
        self._lock = threading.Lock()
        self._cleanup_done = threading.Event()
        self._contexts: dict[str, _StreamContext] = {}

        # Bootstrap a context for every stream already in the config.
        for stream in config_manager.config.streams:
            self._contexts[stream.id] = _StreamContext()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _listening_count(self) -> int:
        """Return the number of streams in LISTENING or RECONNECTING state.

        **Must** be called while ``_lock`` is held.
        """
        return sum(
            1
            for ctx in self._contexts.values()
            if ctx.state in (StreamState.LISTENING, StreamState.RECONNECTING, StreamState.CONNECTING)
        )

    def _get_context(self, stream_id: str) -> _StreamContext:
        """Return the context for *stream_id*, creating one if needed.

        **Must** be called while ``_lock`` is held.
        """
        ctx = self._contexts.get(stream_id)
        if ctx is None:
            ctx = _StreamContext()
            self._contexts[stream_id] = ctx
        return ctx

    def _build_status(
        self, source: StreamSource, ctx: _StreamContext
    ) -> StreamStatus:
        """Build a :class:`StreamStatus` snapshot from a source and context."""
        has_transcript = (
            ctx.session_file_path is not None
            and os.path.isfile(ctx.session_file_path)
        )
        extractor_type = None
        if (
            ctx.state in (StreamState.LISTENING, StreamState.RECONNECTING, StreamState.CONNECTING)
            and ctx.extractor is not None
        ):
            extractor_type = ctx.extractor.extractor_type
        return StreamStatus(
            id=source.id,
            brand=source.brand,
            url=source.url,
            state=ctx.state,
            error_message=ctx.error_message,
            reconnect_attempt=ctx.reconnect_attempt,
            has_transcript=has_transcript,
            extractor_type=extractor_type,
            daily_file_path=ctx.session_file_path,
        )

    def _close_session_file(self, ctx: _StreamContext, brand: str) -> None:
        """Write session end marker and close the file handle.

        **Must** be called while ``_lock`` is held.
        """
        if ctx.session_file_handle is not None:
            try:
                now = datetime.now()
                end_marker = f"=== SESSION PAUSED: {now.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                ctx.session_file_handle.write(end_marker)
                ctx.session_file_handle.flush()
                ctx.session_file_handle.close()
            except OSError as exc:
                logger.warning(
                    "Error closing session file for '%s': %s", brand, exc
                )
            ctx.session_file_handle = None

    def _stop_extractor(self, ctx: _StreamContext, brand: str) -> None:
        """Stop the audio extractor gracefully, logging failures.

        **Must** be called while ``_lock`` is held.
        """
        if ctx.extractor is not None:
            try:
                ctx.extractor.stop()
            except Exception as exc:
                logger.warning(
                    "Error stopping extractor for '%s': %s", brand, exc
                )
            ctx.extractor = None

    def _reset_context(self, ctx: _StreamContext) -> None:
        """Reset a context to the idle state after stopping.

        **Must** be called while ``_lock`` is held.
        """
        ctx.state = StreamState.IDLE
        ctx.worker_thread = None
        ctx.extractor = None
        ctx.stop_event = None
        ctx.reconnect_attempt = None

    # ------------------------------------------------------------------
    # Public interface — individual stream control
    # ------------------------------------------------------------------

    def start_stream(self, stream_id: str) -> StreamStatus:
        """Start listening on a stream.

        Creates a session directory and file, spawns a daemon worker thread,
        and transitions the stream to :attr:`StreamState.LISTENING`.

        Args:
            stream_id: Unique identifier of the stream to start.

        Returns:
            Current :class:`StreamStatus` after the operation.

        Raises:
            KeyError: If *stream_id* is not found in the configuration.
            ConcurrencyLimitError: If the concurrency limit is already reached.
        """
        source = self._config_manager.get_stream(stream_id)  # KeyError if missing

        with self._lock:
            ctx = self._get_context(stream_id)

            # Already active — return current status without changes.
            if ctx.state in (StreamState.LISTENING, StreamState.RECONNECTING, StreamState.CONNECTING):
                return self._build_status(source, ctx)

            # Enforce concurrency limit.
            limit = self._config_manager.config.max_concurrent_streams
            if self._listening_count() >= limit:
                raise ConcurrencyLimitError(
                    f"Concurrency limit of {limit} reached"
                )

            # Open daily transcript file (append mode — one file per day per brand).
            now = datetime.now()
            session_path = generate_daily_path(
                self._config_manager.config.output_base_dir,
                source.brand,
                now,
            )

            try:
                file_handle = open(session_path, "a", encoding="utf-8")
                # Write session start marker for demarcation
                start_marker = f"\n{'='*60}\n=== SESSION STARTED: {now.strftime('%Y-%m-%d %H:%M:%S')} ===\n{'='*60}\n"
                file_handle.write(start_marker)
                file_handle.flush()
            except OSError as exc:
                logger.error(
                    "Failed to create session file for '%s': %s",
                    source.brand,
                    exc,
                )
                ctx.state = StreamState.IDLE
                ctx.error_message = f"Failed to create session file: {exc}"
                return self._build_status(source, ctx)

            # Create audio extractor.
            config = self._config_manager.config
            extractor = FallbackExtractor(
                chunk_duration=config.chunk_duration_seconds,
                timeout_seconds=config.playwright_timeout_seconds,
                auto_click_play=config.playwright_auto_click_play,
            )

            # Prepare context — start as CONNECTING until audio flows.
            stop_event = threading.Event()
            ctx.state = StreamState.CONNECTING
            ctx.session_file_handle = file_handle
            ctx.session_file_path = session_path
            ctx.extractor = extractor
            ctx.stop_event = stop_event
            ctx.error_message = None
            ctx.reconnect_attempt = None

            # Spawn a daemon worker thread.
            worker = threading.Thread(
                target=self._run_worker,
                args=(source, ctx),
                daemon=True,
                name=f"stream-worker-{source.id}",
            )
            ctx.worker_thread = worker
            worker.start()

        return self._build_status(source, ctx)

    def stop_stream(self, stream_id: str) -> StreamStatus:
        """Stop a listening stream.

        Signals the worker thread to stop, waits for it to join, then cleans
        up the extractor and session file.

        Args:
            stream_id: Unique identifier of the stream to stop.

        Returns:
            Current :class:`StreamStatus` after the operation.

        Raises:
            KeyError: If *stream_id* is not found in the configuration.
        """
        source = self._config_manager.get_stream(stream_id)

        with self._lock:
            ctx = self._get_context(stream_id)

            # Not active — nothing to stop.
            if ctx.state not in (
                StreamState.LISTENING,
                StreamState.RECONNECTING,
                StreamState.CONNECTING,
            ):
                return self._build_status(source, ctx)

            # Signal the worker to stop.
            if ctx.stop_event:
                ctx.stop_event.set()

            thread = ctx.worker_thread

        # Wait outside the lock to avoid blocking other operations.
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

        # Finalize cleanup under the lock.
        with self._lock:
            self._stop_extractor(ctx, source.brand)
            self._close_session_file(ctx, source.brand)
            self._reset_context(ctx)

        return self._build_status(source, ctx)

    # ------------------------------------------------------------------
    # Public interface — bulk operations
    # ------------------------------------------------------------------

    def start_all(self) -> dict:
        """Start all idle streams up to the concurrency limit.

        Returns:
            A dict with three keys:

            * ``started`` — list of stream IDs that were started successfully.
            * ``failed`` — list of dicts ``{"id": ..., "error": ...}`` for
              streams that failed to start.
            * ``skipped`` — list of stream IDs that were skipped (already
              active or concurrency limit reached).
        """
        started: list[str] = []
        failed: list[dict] = []
        skipped: list[str] = []

        for source in self._config_manager.config.streams:
            with self._lock:
                ctx = self._contexts.get(source.id)
                if ctx and ctx.state != StreamState.IDLE:
                    skipped.append(source.id)
                    continue

            try:
                self.start_stream(source.id)
                started.append(source.id)
            except ConcurrencyLimitError:
                skipped.append(source.id)
            except Exception as exc:
                failed.append({"id": source.id, "error": str(exc)})

        return {"started": started, "failed": failed, "skipped": skipped}

    def stop_all(self) -> list[StreamStatus]:
        """Stop all streams that are currently listening.

        Returns:
            List of :class:`StreamStatus` snapshots for the stopped streams.
        """
        statuses: list[StreamStatus] = []

        ids_to_stop: list[str] = []
        with self._lock:
            for source in self._config_manager.config.streams:
                ctx = self._contexts.get(source.id)
                if ctx and ctx.state in (
                    StreamState.LISTENING,
                    StreamState.RECONNECTING,
                ):
                    ids_to_stop.append(source.id)

        for sid in ids_to_stop:
            status = self.stop_stream(sid)
            statuses.append(status)

        return statuses

    # ------------------------------------------------------------------
    # Public interface — status queries
    # ------------------------------------------------------------------

    def get_all_statuses(self) -> list[StreamStatus]:
        """Return the current status of every configured stream."""
        statuses: list[StreamStatus] = []
        with self._lock:
            for source in self._config_manager.config.streams:
                ctx = self._get_context(source.id)
                statuses.append(self._build_status(source, ctx))
        return statuses

    def is_stream_listening(self, stream_id: str) -> bool:
        """Check whether a stream is currently in LISTENING or RECONNECTING state.

        Used by the API layer to block edit/remove operations on active
        streams per requirements 2.3 and 3.3.
        """
        with self._lock:
            ctx = self._contexts.get(stream_id)
            if ctx is None:
                return False
            return ctx.state in (
                StreamState.LISTENING,
                StreamState.RECONNECTING,
                StreamState.CONNECTING,
            )

    def get_download_path(self, stream_id: str) -> Optional[str]:
        """Return the path to the active or most-recent session file.

        For a currently-listening stream the active session file is returned.
        For an idle stream the most recent transcript file (by creation time)
        is located by scanning the brand's output directory.

        Args:
            stream_id: Unique identifier of the stream.

        Returns:
            Absolute path to the session file, or ``None`` if no transcript
            exists for this stream.

        Raises:
            KeyError: If *stream_id* is not found in the configuration.
        """
        source = self._config_manager.get_stream(stream_id)

        # Prefer the active session file if it exists on disk.
        with self._lock:
            ctx = self._contexts.get(stream_id)
            if (
                ctx
                and ctx.session_file_path
                and os.path.isfile(ctx.session_file_path)
            ):
                return ctx.session_file_path

        # Fall back to the most-recent transcript by creation time.
        brand_dir = os.path.join(
            self._config_manager.config.output_base_dir,
            source.brand,
        )
        if not os.path.isdir(brand_dir):
            return None

        latest_path: Optional[str] = None
        latest_ctime: float = 0.0

        for date_dir_name in os.listdir(brand_dir):
            date_dir = os.path.join(brand_dir, date_dir_name)
            if not os.path.isdir(date_dir):
                continue
            for filename in os.listdir(date_dir):
                if not filename.endswith(".txt"):
                    continue
                fpath = os.path.join(date_dir, filename)
                try:
                    ctime = os.path.getctime(fpath)
                except OSError:
                    continue
                if ctime > latest_ctime:
                    latest_ctime = ctime
                    latest_path = fpath

        return latest_path

    # ------------------------------------------------------------------
    # State-change callback (used by StreamWorker)
    # ------------------------------------------------------------------

    def on_state_change(
        self,
        stream_id: str,
        state: StreamState,
        error_message: Optional[str] = None,
        reconnect_attempt: Optional[int] = None,
    ) -> None:
        """Callback for StreamWorker to report state transitions (thread-safe).

        Args:
            stream_id: ID of the stream whose state changed.
            state: The new :class:`StreamState`.
            error_message: Optional error description.
            reconnect_attempt: Current reconnection attempt number (1–5),
                or ``None`` if not reconnecting.
        """
        with self._lock:
            ctx = self._contexts.get(stream_id)
            if ctx is None:
                return
            ctx.state = state
            ctx.error_message = error_message
            ctx.reconnect_attempt = reconnect_attempt

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Terminate all subprocesses, close session files, delete temp files.

        Called on application shutdown.  Must complete within 10 seconds:
        up to 5 s for thread joins, then remaining time for extractor and
        file-handle cleanup.

        Idempotent — safe to call from multiple shutdown paths (lifespan,
        atexit, signal handlers) without deadlocking or double-cleaning.
        """
        if self._cleanup_done.is_set():
            logger.debug("StreamManager cleanup: already completed, skipping.")
            return
        self._cleanup_done.set()

        logger.info("StreamManager cleanup: stopping all streams …")
        start = time.monotonic()

        # 1. Signal every worker to stop.
        with self._lock:
            for ctx in self._contexts.values():
                if ctx.stop_event:
                    ctx.stop_event.set()

        # 2. Collect live worker threads.
        threads: list[threading.Thread] = []
        with self._lock:
            for ctx in self._contexts.values():
                if ctx.worker_thread and ctx.worker_thread.is_alive():
                    threads.append(ctx.worker_thread)

        # 3. Join threads with a ~5 s budget spread across all threads.
        if threads:
            per_thread = max(5.0 / len(threads), 0.5)
            for thread in threads:
                remaining = max(10.0 - (time.monotonic() - start), 0.1)
                thread.join(timeout=min(per_thread, remaining))

        # 4. Force-stop extractors and close session files.
        with self._lock:
            for stream_id, ctx in self._contexts.items():
                # Resolve brand name for log messages (best-effort).
                brand = stream_id
                try:
                    brand = self._config_manager.get_stream(stream_id).brand
                except KeyError:
                    pass

                self._stop_extractor(ctx, brand)
                self._close_session_file(ctx, brand)
                self._reset_context(ctx)

        elapsed = time.monotonic() - start
        logger.info(
            "StreamManager cleanup complete in %.1f s.", elapsed
        )

    # ------------------------------------------------------------------
    # Worker entry point
    # ------------------------------------------------------------------

    def _run_worker(
        self, source: StreamSource, ctx: _StreamContext
    ) -> None:
        """Entry point for the worker thread.

        Creates a :class:`StreamWorker` and runs its processing loop.
        """
        worker = StreamWorker(
            source=source,
            ctx=ctx,
            transcriber=self._transcriber,
            on_state_change=self.on_state_change,
            chunk_duration=self._config_manager.config.chunk_duration_seconds,
        )
        worker.run()
