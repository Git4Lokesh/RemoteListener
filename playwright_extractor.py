"""Playwright-based audio extractor using stream URL discovery + ffmpeg.

Uses Playwright to launch a headless Chromium browser, navigate to a page,
and intercept network responses to discover the actual media stream URL
(HLS .m3u8 or DASH .mpd).  Once the stream URL is found the browser is
closed immediately and the URL is piped into ffmpeg for audio extraction,
producing 16 kHz mono 16-bit PCM WAV chunks identical to those from
StreamlinkFFmpegExtractor.

Requirements: 16.1, 16.2, 16.3, 16.4
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from extractor import (
    AudioExtractor,
    BYTES_PER_SECOND,
    CHANNELS,
    SAMPLE_RATE,
    _write_wav_file,
)

logger = logging.getLogger(__name__)

# Ad / tracking domains whose .m3u8 URLs should be ignored
_AD_DOMAINS = (
    "2mdn.net",
    "doubleclick",
    "imasdk",
    "googlesyndication",
    "googleads",
)


class PlaywrightExtractor(AudioExtractor):
    """Extract audio by discovering the stream URL with Playwright, then
    piping it through ffmpeg.

    The browser is used solely for network interception — once the first
    qualifying media URL (.m3u8 / .mpd) is found, the browser is closed
    and ffmpeg reads directly from that URL.  This avoids the complexity
    of Web Audio API injection while still supporting sites that require
    JavaScript rendering to reveal the actual stream endpoint.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        chunk_duration: int = 10,
        timeout_seconds: int = 15,
        auto_click_play: bool = True,
        temp_dir: Optional[str] = None,
    ) -> None:
        """Configure the browser-based audio extractor.

        Args:
            chunk_duration: Duration of each audio chunk in seconds.
            timeout_seconds: Maximum seconds to wait for a media stream
                URL to appear in network traffic after navigating to the
                page.
            auto_click_play: Whether to attempt clicking common play-button
                selectors on the page to trigger media loading.
            temp_dir: Optional base directory for temporary chunk files.
                A subdirectory is created via ``tempfile.mkdtemp``.  If
                *None*, the system default temp location is used.
        """
        self._chunk_duration = chunk_duration
        self._timeout_seconds = timeout_seconds
        self._auto_click_play = auto_click_play
        self._base_temp_dir = temp_dir

        # Derived constant: bytes per full audio chunk
        self._chunk_bytes = chunk_duration * BYTES_PER_SECOND  # 32000 B/s

        # Runtime state — set during start(), cleared on stop()
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._stream_url: Optional[str] = None
        self._temp_dir: Optional[str] = None
        self._chunk_counter: int = 0

    # ------------------------------------------------------------------
    # AudioExtractor interface
    # ------------------------------------------------------------------

    @property
    def extractor_type(self) -> str:
        """Return the extractor type identifier for dashboard display."""
        return "browser"

    def start(self, url: str) -> None:
        """Discover the media stream URL via Playwright, then start ffmpeg.

        1. Launch headless Chromium, intercept network responses to find
           an .m3u8 or .mpd stream URL.
        2. Close the browser.
        3. Start ffmpeg reading from the discovered stream URL.
        4. Create a temp directory for WAV chunk files.

        Raises:
            ConnectionError: If Playwright is not installed, the browser
                fails to launch, no stream URL is discovered within the
                timeout, or ffmpeg fails to start.
        """
        # Reset mutable state for a fresh session
        self._chunk_counter = 0
        self._stream_url = None

        # Step 1 — Discover the media stream URL via Playwright
        self._stream_url = self._discover_stream_url(url, self._timeout_seconds)
        logger.info(
            "Discovered stream URL for %s: %s", url, self._stream_url
        )

        # Step 2 — Create temp directory for WAV chunks
        self._temp_dir = tempfile.mkdtemp(
            prefix="rlistener_pw_", dir=self._base_temp_dir
        )

        # Step 3 — Start ffmpeg to read from the discovered stream URL
        try:
            # Pass browser-like headers so CDNs allow access
            ffmpeg_headers = (
                "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\r\n"
                f"Referer: {url}\r\n"
                f"Origin: {url.split('/')[0]}//{url.split('/')[2]}\r\n"
            )
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-headers", ffmpeg_headers,
                    "-i", self._stream_url,
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ac", str(CHANNELS),
                    "-ar", str(SAMPLE_RATE),
                    "-v", "quiet",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._cleanup_temp_dir()
            raise ConnectionError(
                "ffmpeg not found on PATH. Install ffmpeg to use the "
                "browser extractor."
            ) from exc
        except OSError as exc:
            self._cleanup_temp_dir()
            raise ConnectionError(
                f"Failed to start ffmpeg for stream URL: {exc}"
            ) from exc

    def read(self) -> Optional[str]:
        """Read the next audio chunk from ffmpeg stdout.

        Blocks until ``chunk_duration × 32000`` bytes are available, or
        until the stream ends (returning a shorter final chunk or ``None``).

        Returns:
            Path to a temporary .wav file, or ``None`` on graceful stream
            end.

        Raises:
            ConnectionError: If reading from ffmpeg fails unexpectedly.
        """
        if self._ffmpeg_proc is None or self._ffmpeg_proc.stdout is None:
            return None

        try:
            data = self._ffmpeg_proc.stdout.read(self._chunk_bytes)
        except (OSError, ValueError) as exc:
            raise ConnectionError(
                f"Error reading from ffmpeg pipeline: {exc}"
            ) from exc

        # Stream ended gracefully — no more data
        if not data:
            return None

        # Write raw PCM bytes to a WAV temp file
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
        """Stop ffmpeg and clean up temporary files.

        Sends SIGTERM first; if ffmpeg hasn't exited after 5 seconds it
        receives SIGKILL.  The temp directory is removed afterwards.
        Calling ``stop()`` multiple times is safe (idempotent).
        """
        self._kill_ffmpeg()
        self._cleanup_temp_dir()

    # ------------------------------------------------------------------
    # Internal: stream URL discovery via Playwright
    # ------------------------------------------------------------------

    def _discover_stream_url(self, page_url: str, timeout: int) -> str:
        """Launch a headless browser, navigate to *page_url*, and intercept
        network responses to find a media stream URL.

        The browser is closed as soon as a qualifying URL is found or the
        timeout expires.

        Args:
            page_url: The web page URL to navigate to.
            timeout: Maximum seconds to wait for a stream URL.

        Returns:
            The discovered media stream URL.

        Raises:
            ConnectionError: If Playwright is not installed, the browser
                cannot launch, navigation fails, or no stream URL is found.
        """
        try:
            from playwright.sync_api import sync_playwright  # noqa: WPS433
        except ImportError as exc:
            raise ConnectionError(
                "Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            ) from exc

        collected_urls: list[str] = []
        playwright_instance = None
        browser = None

        try:
            playwright_instance = sync_playwright().start()
            browser = playwright_instance.chromium.launch(
                headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context()
            page = context.new_page()

            # --- Network response listener ---
            def _on_response(response):
                """Collect qualifying media stream URLs from responses."""
                try:
                    resp_url = response.url
                    if not self._is_stream_url(resp_url):
                        return
                    if self._is_ad_url(resp_url):
                        logger.debug("Skipping ad URL: %s", resp_url)
                        return
                    collected_urls.append(resp_url)
                    logger.debug("Collected stream URL: %s", resp_url)
                except Exception:
                    pass

            page.on("response", _on_response)

            # Navigate to the page
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                msg = str(exc)
                if "Executable doesn't exist" in msg or "browserType.launch" in msg:
                    raise ConnectionError(
                        "Playwright browsers not installed. "
                        "Run: playwright install chromium"
                    ) from exc
                if "timeout" in msg.lower():
                    raise ConnectionError(
                        f"Navigation timeout for URL {page_url}"
                    ) from exc
                raise ConnectionError(
                    f"Failed to navigate to {page_url}: {msg}"
                ) from exc

            # Wait a couple seconds for initial network activity
            page.wait_for_timeout(2000)

            # Try clicking play / unmute to trigger media loading
            if self._auto_click_play:
                self._try_click_play(page)

            # Wait for a stream URL to appear
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if collected_urls:
                    best = self._pick_best_url(collected_urls)
                    logger.info("Selected stream URL: %s", best)
                    return best
                page.wait_for_timeout(500)

            # Timeout — no stream URL found
            raise ConnectionError(
                f"No media stream URL discovered within {timeout}s "
                f"for page {page_url}"
            )

        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError(
                f"Browser stream URL discovery failed: {exc}"
            ) from exc
        finally:
            # Always close the browser, no matter what
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if playwright_instance is not None:
                try:
                    playwright_instance.stop()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Internal: URL classification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stream_url(url: str) -> bool:
        """Return True if *url* looks like a media stream manifest."""
        url_lower = url.lower().split("?")[0]  # strip query params
        return url_lower.endswith(".m3u8") or ".m3u8" in url.lower() or \
               url_lower.endswith(".mpd") or ".mpd" in url.lower()

    @staticmethod
    def _is_ad_url(url: str) -> bool:
        """Return True if *url* belongs to a known ad / tracking domain."""
        url_lower = url.lower()
        return any(domain in url_lower for domain in _AD_DOMAINS)

    # CDN domains that block non-browser access (TLS fingerprint checks)
    _BLOCKED_CDNS = ("cdn.jio.com",)

    @staticmethod
    def _pick_best_url(urls: list[str]) -> str:
        """Pick the best stream URL from the collected candidates.

        Prefers master/index playlists from accessible CDNs (e.g. Akamai)
        over CDNs that block non-browser access (e.g. Jio).
        """
        # Score each URL: higher is better
        def score(url):
            lower = url.lower()
            s = 0
            if "index.m3u8" in lower or "master" in lower:
                s += 10  # Prefer master playlists
            # Penalise CDNs known to block ffmpeg
            if any(cdn in lower for cdn in PlaywrightExtractor._BLOCKED_CDNS):
                s -= 20
            return s

        ranked = sorted(urls, key=score, reverse=True)
        return ranked[0]

    # ------------------------------------------------------------------
    # Internal: play-button click heuristic
    # ------------------------------------------------------------------

    @staticmethod
    def _try_click_play(page) -> None:
        """Attempt to click play buttons and unmute video elements.

        Waits 2 seconds for the page to settle, then:
        1. Tries to click common play-button selectors
        2. Unmutes all video/audio elements via JavaScript
        3. Tries to click common unmute/volume button selectors

        Failures are silently ignored — many pages auto-play and have no
        visible play button.
        """
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # --- Click player container areas (triggers video load on some sites) ---
        container_selectors = [
            ".video-stage",
            "#livePlayer",
            ".vgrPlayerContainer",
            '[class*="player-container"]',
            "#vp",
        ]
        for selector in container_selectors:
            try:
                page.click(selector, timeout=1500)
                logger.debug("Clicked container: %s", selector)
            except Exception:
                continue

        page.wait_for_timeout(2000)

        # --- Click play buttons ---
        play_selectors = [
            'button[aria-label*="play" i]',
            ".play-button",
            "video",
            '[class*="play-btn"]',
            "button.ytp-large-play-button",
        ]
        for selector in play_selectors:
            try:
                page.click(selector, timeout=2000)
                logger.debug("Clicked play selector: %s", selector)
                break
            except Exception:
                continue

        # --- Unmute all media elements via JavaScript ---
        try:
            page.evaluate("""() => {
                document.querySelectorAll('video').forEach(v => {
                    v.muted = false;
                    v.volume = 1.0;
                    if (v.paused) v.play().catch(() => {});
                });
                document.querySelectorAll('audio').forEach(a => {
                    a.muted = false;
                    a.volume = 1.0;
                    if (a.paused) a.play().catch(() => {});
                });
            }""")
            logger.debug("Unmuted media elements via JS")
        except Exception:
            pass

        # --- Click unmute/volume buttons ---
        unmute_selectors = [
            'button[aria-label*="unmute" i]',
            'button[aria-label*="mute" i]',
            '[class*="unmute"]',
            '[class*="volume"]',
            'button.ytp-mute-button',
        ]
        for selector in unmute_selectors:
            try:
                page.click(selector, timeout=1000)
                logger.debug("Clicked unmute selector: %s", selector)
                break
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Internal: ffmpeg process management
    # ------------------------------------------------------------------

    def _kill_ffmpeg(self) -> None:
        """Gracefully terminate, then forcefully kill the ffmpeg process."""
        proc = self._ffmpeg_proc
        if proc is None:
            return

        # SIGTERM first
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass

        # Wait up to 5 seconds
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()  # SIGKILL
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                logger.warning(
                    "Could not kill ffmpeg process pid=%s", proc.pid
                )

        # Close remaining file descriptors
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

        self._ffmpeg_proc = None

    def _cleanup_temp_dir(self) -> None:
        """Remove the temporary directory and all chunk files inside it."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
            except OSError as exc:
                logger.warning(
                    "Failed to clean up temp dir %s: %s",
                    self._temp_dir,
                    exc,
                )
        self._temp_dir = None
