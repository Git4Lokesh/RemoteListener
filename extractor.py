"""Audio extractor layer with pluggable adapter pattern.

Defines the AudioExtractor abstract base class and the default
StreamlinkFFmpegExtractor implementation that pipes streamlink output
through ffmpeg to produce 16kHz mono PCM WAV chunks.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 16.1, 16.2, 16.3, 16.4
"""

import logging
import os
import shutil
import signal
import struct
import subprocess
import tempfile
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

# Audio format constants
SAMPLE_RATE = 16000       # 16 kHz
CHANNELS = 1              # mono
BITS_PER_SAMPLE = 16      # 16-bit PCM
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE // 8)  # 32000


def _write_wav_file(path: str, pcm_data: bytes) -> None:
    """Write raw PCM data to a WAV file with the correct header.

    The WAV header describes 16-bit signed PCM, mono, 16 kHz audio to match
    the ffmpeg output format used by the extractor pipeline.
    """
    data_size = len(pcm_data)
    # RIFF header (44 bytes total)
    # file_size = 36 + data_size  (total file size minus 8 bytes for RIFF/size)
    header = struct.pack(
        "<4sI4s"       # RIFF chunk descriptor
        "4sIHHIIHH"    # fmt sub-chunk
        "4sI",         # data sub-chunk header
        b"RIFF",
        36 + data_size,        # ChunkSize
        b"WAVE",
        b"fmt ",
        16,                    # Subchunk1Size (PCM = 16)
        1,                     # AudioFormat (PCM = 1)
        CHANNELS,              # NumChannels
        SAMPLE_RATE,           # SampleRate
        BYTES_PER_SECOND,      # ByteRate
        CHANNELS * (BITS_PER_SAMPLE // 8),  # BlockAlign
        BITS_PER_SAMPLE,       # BitsPerSample
        b"data",
        data_size,             # Subchunk2Size
    )
    with open(path, "wb") as f:
        f.write(header)
        f.write(pcm_data)


# ---------------------------------------------------------------------------
# Abstract base class (Req 16.1)
# ---------------------------------------------------------------------------

class AudioExtractor(ABC):
    """Base interface for audio extraction adapters.

    Subclasses must implement start/read/stop to provide sequential audio
    chunks from a live stream URL. This enables plugging in alternative
    extractors (e.g., headless browser) without changing the transcription
    or stream management layers.
    """

    @abstractmethod
    def start(self, url: str) -> None:
        """Begin extracting audio from the given stream URL.

        Raises:
            ConnectionError: If the source is unreachable or unsupported.
        """

    @abstractmethod
    def read(self) -> Optional[str]:
        """Read the next audio chunk, blocking until ready.

        Returns:
            Path to a temporary .wav file containing the chunk, or ``None``
            if the stream ended gracefully.

        Raises:
            ConnectionError: On network interruption during extraction.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop extraction and release all resources.

        Implementations must kill child processes and delete temporary files.
        """


# ---------------------------------------------------------------------------
# Default implementation: streamlink + ffmpeg (Req 16.2)
# ---------------------------------------------------------------------------

class StreamlinkFFmpegExtractor(AudioExtractor):
    """Extract audio via *streamlink* piped into *ffmpeg*.

    The subprocess pipeline is::

        streamlink {url} best -O
            │ stdout → stdin
        ffmpeg -i pipe:0 -f s16le -acodec pcm_s16le -ac 1 -ar 16000 -v quiet pipe:1
            │ stdout → Python reads fixed-size byte blocks

    Each block of ``chunk_duration × 32000`` bytes is wrapped with a WAV
    header and written to a temporary file whose path is returned by
    :meth:`read`.
    """

    def __init__(
        self,
        chunk_duration: int = 10,
        temp_dir: Optional[str] = None,
    ) -> None:
        """Configure the extractor.

        Args:
            chunk_duration: Duration of each audio chunk in seconds.
            temp_dir: Optional base temp directory. A subdirectory is created
                via ``tempfile.mkdtemp(prefix="rlistener_")``. If *None*, the
                system default temp location is used.
        """
        self._chunk_duration = chunk_duration
        self._chunk_bytes = chunk_duration * BYTES_PER_SECOND
        self._base_temp_dir = temp_dir
        self._temp_dir: Optional[str] = None
        self._streamlink_proc: Optional[subprocess.Popen] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._chunk_counter: int = 0

    # -- Public interface ---------------------------------------------------

    @property
    def extractor_type(self) -> str:
        """Return the extractor type identifier for dashboard display."""
        return "streamlink"

    def start(self, url: str) -> None:
        """Launch the streamlink → ffmpeg subprocess chain.

        Raises:
            ConnectionError: If streamlink fails to extract the stream.
        """
        # Create a fresh temp directory for this session's chunks
        self._temp_dir = tempfile.mkdtemp(
            prefix="rlistener_", dir=self._base_temp_dir
        )
        self._chunk_counter = 0

        try:
            # 1. streamlink: extract media stream and pipe to stdout
            self._streamlink_proc = subprocess.Popen(
                ["streamlink", url, "best", "-O"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # 2. ffmpeg: convert piped input to raw 16-bit PCM 16 kHz mono
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-i", "pipe:0",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ac", str(CHANNELS),
                    "-ar", str(SAMPLE_RATE),
                    "-v", "quiet",
                    "pipe:1",
                ],
                stdin=self._streamlink_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Allow streamlink to receive SIGPIPE if ffmpeg exits
            if self._streamlink_proc.stdout:
                self._streamlink_proc.stdout.close()

        except FileNotFoundError as exc:
            self._cleanup_temp_dir()
            raise ConnectionError(
                f"Required command not found: {exc.filename}"
            ) from exc
        except OSError as exc:
            self._cleanup_temp_dir()
            raise ConnectionError(
                f"Failed to start audio extraction: {exc}"
            ) from exc

        # Quick sanity check: if streamlink already exited with an error,
        # the pipe is broken and we should report immediately.
        ret = self._streamlink_proc.poll()
        if ret is not None and ret != 0:
            stderr_out = ""
            if self._streamlink_proc.stderr:
                try:
                    stderr_out = self._streamlink_proc.stderr.read().decode(
                        errors="replace"
                    )
                except Exception:
                    pass
            self._kill_processes()
            self._cleanup_temp_dir()
            raise ConnectionError(
                f"streamlink exited with code {ret} for URL {url}: "
                f"{stderr_out.strip()}"
            )

    def read(self) -> Optional[str]:
        """Read the next audio chunk from ffmpeg stdout.

        Blocks until ``chunk_duration × 32000`` bytes are available, or until
        the stream ends (returning a shorter final chunk or ``None``).

        Returns:
            Path to a temporary .wav file, or ``None`` on graceful stream end.

        Raises:
            ConnectionError: If the extraction pipeline breaks unexpectedly.
        """
        if self._ffmpeg_proc is None or self._ffmpeg_proc.stdout is None:
            return None

        try:
            data = self._ffmpeg_proc.stdout.read(self._chunk_bytes)
        except (OSError, ValueError) as exc:
            raise ConnectionError(
                f"Error reading from audio pipeline: {exc}"
            ) from exc

        # Stream ended gracefully — no more data at all
        if not data:
            return None

        # Build a temp .wav file from the raw PCM bytes
        chunk_path = os.path.join(
            self._temp_dir or tempfile.gettempdir(),
            f"chunk_{self._chunk_counter:06d}.wav",
        )
        self._chunk_counter += 1

        try:
            _write_wav_file(chunk_path, data)
        except OSError as exc:
            raise ConnectionError(
                f"Failed to write temp chunk file: {exc}"
            ) from exc

        return chunk_path

    def stop(self) -> None:
        """Terminate subprocesses and clean up temporary files.

        Sends SIGTERM first; if a process hasn't exited after 5 seconds it
        receives SIGKILL.
        """
        self._kill_processes()
        self._cleanup_temp_dir()

    # -- Internal helpers ---------------------------------------------------

    def _kill_processes(self) -> None:
        """Gracefully terminate, then forcefully kill subprocess chain."""
        for proc in (self._ffmpeg_proc, self._streamlink_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()  # SIGTERM
            except OSError:
                pass

        # Wait up to 5 seconds for both to exit
        for proc in (self._ffmpeg_proc, self._streamlink_proc):
            if proc is None:
                continue
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill
                try:
                    proc.kill()  # SIGKILL
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    logger.warning(
                        "Could not kill subprocess pid=%s", proc.pid
                    )

        # Close any remaining file descriptors
        for proc in (self._ffmpeg_proc, self._streamlink_proc):
            if proc is None:
                continue
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

        self._ffmpeg_proc = None
        self._streamlink_proc = None

    def _cleanup_temp_dir(self) -> None:
        """Remove the temporary directory and all chunk files inside it."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
            except OSError as exc:
                logger.warning(
                    "Failed to clean up temp dir %s: %s",
                    self._temp_dir, exc,
                )
        self._temp_dir = None


# ---------------------------------------------------------------------------
# Fallback implementation: streamlink → browser (Req 16.3, 16.4)
# ---------------------------------------------------------------------------

class FallbackExtractor(AudioExtractor):
    """Tries StreamlinkFFmpegExtractor first, falls back to PlaywrightExtractor.

    This composite extractor implements the adapter pattern by attempting the
    lightweight streamlink+ffmpeg pipeline first.  If that fails (e.g. the
    site requires JavaScript rendering or DRM), it automatically falls back
    to the browser-based PlaywrightExtractor.  The active extractor is
    transparent to callers — ``read()``, ``stop()``, and ``extractor_type``
    all delegate to whichever child extractor succeeded.
    """

    def __init__(
        self,
        chunk_duration: int = 10,
        timeout_seconds: int = 15,
        auto_click_play: bool = True,
        temp_dir: Optional[str] = None,
    ) -> None:
        """Configure the fallback extractor.

        All parameters are forwarded to the child extractors when they are
        created during :meth:`start`.

        Args:
            chunk_duration: Duration of each audio chunk in seconds.
            timeout_seconds: Maximum seconds the browser extractor waits
                for initial audio data.
            auto_click_play: Whether the browser extractor attempts to click
                common play-button selectors.
            temp_dir: Optional base directory for temporary chunk files.
        """
        self._chunk_duration = chunk_duration
        self._timeout_seconds = timeout_seconds
        self._auto_click_play = auto_click_play
        self._temp_dir = temp_dir
        self._active: Optional[AudioExtractor] = None

    # -- Public interface ---------------------------------------------------

    @property
    def extractor_type(self) -> str:
        """Return the active child extractor's type, or ``"unknown"``."""
        if self._active is not None:
            return self._active.extractor_type
        return "unknown"

    def start(self, url: str) -> None:
        """Try streamlink first, then fall back to the browser extractor.

        Raises:
            ConnectionError: If both extractors fail.  The message includes
                the error from each attempt.
        """
        # --- Attempt 1: streamlink + ffmpeg ---
        sl_extractor = StreamlinkFFmpegExtractor(
            chunk_duration=self._chunk_duration,
            temp_dir=self._temp_dir,
        )
        try:
            sl_extractor.start(url)
            # Verify streamlink actually connected — wait briefly and check
            # if the subprocess is still alive.  Some URLs launch the process
            # successfully but produce no audio data (e.g. custom JS players).
            import time
            time.sleep(3)
            sl_proc = getattr(sl_extractor, "_streamlink_proc", None)
            ff_proc = getattr(sl_extractor, "_ffmpeg_proc", None)
            if sl_proc and sl_proc.poll() is not None:
                raise ConnectionError(
                    f"Streamlink exited early (code {sl_proc.returncode}) — "
                    f"URL likely not supported"
                )
            if ff_proc and ff_proc.poll() is not None:
                raise ConnectionError(
                    f"ffmpeg exited early (code {ff_proc.returncode}) — "
                    f"no audio data from streamlink"
                )
            self._active = sl_extractor
            logger.info("Streamlink extractor succeeded for %s", url)
            return
        except ConnectionError as sl_err:
            sl_error_msg = str(sl_err)
            logger.warning(
                "Streamlink extractor failed for %s: %s", url, sl_error_msg
            )
            sl_extractor.stop()

        # --- Attempt 2: headless browser ---
        from playwright_extractor import PlaywrightExtractor  # noqa: WPS433

        pw_extractor = PlaywrightExtractor(
            chunk_duration=self._chunk_duration,
            timeout_seconds=self._timeout_seconds,
            auto_click_play=self._auto_click_play,
            temp_dir=self._temp_dir,
        )
        try:
            pw_extractor.start(url)
            self._active = pw_extractor
            logger.info("Browser extractor succeeded for %s", url)
            return
        except ConnectionError as pw_err:
            pw_error_msg = str(pw_err)
            logger.warning(
                "Browser extractor failed for %s: %s", url, pw_error_msg
            )
            raise ConnectionError(
                f"All extractors failed for {url}: "
                f"streamlink: {sl_error_msg}; browser: {pw_error_msg}"
            ) from pw_err

    def read(self) -> Optional[str]:
        """Delegate to the active child extractor's read().

        Returns:
            Path to a temporary ``.wav`` file, or ``None`` if the stream
            ended gracefully.

        Raises:
            ConnectionError: If the active extractor encounters an error.
        """
        if self._active is None:
            return None
        return self._active.read()

    def stop(self) -> None:
        """Delegate to the active child extractor's stop()."""
        if self._active is not None:
            self._active.stop()
            self._active = None
