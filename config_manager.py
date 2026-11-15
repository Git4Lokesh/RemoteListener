"""Configuration management for the Multi-Stream Listener & Transcriber.

Handles reading, writing, and validation of config.yaml with thread-safe
file access and graceful fallback to defaults for invalid values.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

from models import AppConfig, StreamSource

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages the config.yaml lifecycle: load, validate, persist, and CRUD for streams."""

    def __init__(self, config_path: str = "config.yaml"):
        self._config_path = Path(config_path)
        self._lock = threading.Lock()
        self._config = self._load_or_create()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def config(self) -> AppConfig:
        """Return the current in-memory configuration."""
        return self._config

    def reload(self) -> AppConfig:
        """Re-read config.yaml from disk and return the parsed config."""
        with self._lock:
            self._config = self._read_and_validate()
        return self._config

    def save(self) -> None:
        """Write the current in-memory config to config.yaml (thread-safe)."""
        with self._lock:
            self._write_config(self._config)

    # ------------------------------------------------------------------
    # Stream CRUD
    # ------------------------------------------------------------------

    def add_stream(self, brand: str, url: str) -> StreamSource:
        """Validate, create, persist, and return a new stream source.

        Raises ValueError on validation failure, IOError on persist failure.
        """
        self._validate_brand(brand, max_length=100)
        self._validate_url(url, max_length=2048)

        with self._lock:
            # Check URL uniqueness under lock
            stripped_url = url.strip()
            for s in self._config.streams:
                if s.url == stripped_url:
                    raise ValueError(f"URL already configured: {stripped_url}")

            stream = StreamSource(
                id=uuid.uuid4().hex[:8],
                brand=brand.strip(),
                url=stripped_url,
            )
            self._config.streams.append(stream)
            try:
                self._write_config(self._config)
            except Exception:
                self._config.streams.remove(stream)
                raise IOError("Failed to persist new stream source to config.yaml")

        return stream

    def update_stream(
        self, stream_id: str, brand: Optional[str] = None, url: Optional[str] = None
    ) -> StreamSource:
        """Validate, update, persist, and return the updated stream source.

        Raises KeyError if not found, ValueError on validation failure.
        """
        stream = self.get_stream(stream_id)

        if brand is not None:
            self._validate_brand(brand, max_length=50)
        if url is not None:
            self._validate_url(url, max_length=2048)

        with self._lock:
            # Check brand uniqueness under lock
            if brand is not None:
                stripped_brand = brand.strip()
                for s in self._config.streams:
                    if s.id != stream_id and s.brand == stripped_brand:
                        raise ValueError(
                            f"Brand name already in use: {stripped_brand}"
                        )

            old_brand, old_url = stream.brand, stream.url
            if brand is not None:
                stream.brand = brand.strip()
            if url is not None:
                stream.url = url.strip()

            try:
                self._write_config(self._config)
            except Exception:
                stream.brand, stream.url = old_brand, old_url
                raise IOError(
                    "Failed to persist updated stream source to config.yaml"
                )

        return stream

    def remove_stream(self, stream_id: str) -> None:
        """Remove a stream by ID and persist. Raises KeyError if not found."""
        stream = self.get_stream(stream_id)

        with self._lock:
            self._config.streams.remove(stream)
            try:
                self._write_config(self._config)
            except Exception:
                self._config.streams.append(stream)
                raise IOError("Failed to persist stream removal to config.yaml")

    def get_stream(self, stream_id: str) -> StreamSource:
        """Look up a stream by ID. Raises KeyError if not found."""
        for s in self._config.streams:
            if s.id == stream_id:
                return s
        raise KeyError(f"Stream not found: {stream_id}")

    # ------------------------------------------------------------------
    # Config loading and validation
    # ------------------------------------------------------------------

    def _load_or_create(self) -> AppConfig:
        """Load config from file or create a default one if missing."""
        if not self._config_path.exists():
            logger.info("config.yaml not found — creating with defaults")
            try:
                config = AppConfig()
                self._write_config(config)
                return config
            except OSError as exc:
                logger.error("Failed to create config.yaml: %s", exc)
                return AppConfig()

        return self._read_and_validate()

    def _read_and_validate(self) -> AppConfig:
        """Read config.yaml, validate each field, and return an AppConfig."""
        try:
            raw_text = self._config_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read config.yaml: %s", exc)
            return AppConfig()

        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            logger.error("Syntactically invalid YAML in config.yaml: %s", exc)
            return AppConfig()

        if not isinstance(data, dict):
            logger.error("config.yaml root is not a mapping — using all defaults")
            return AppConfig()

        defaults = AppConfig()

        max_cs = self._validate_int_field(
            data, "max_concurrent_streams", 1, 20, defaults.max_concurrent_streams
        )
        chunk_ds = self._validate_int_field(
            data, "chunk_duration_seconds", 5, 300, defaults.chunk_duration_seconds
        )
        whisper_m = self._validate_str_field(
            data, "whisper_model", 64, defaults.whisper_model
        )
        output_dir = self._validate_str_field(
            data, "output_base_dir", 512, defaults.output_base_dir
        )
        pw_timeout = self._validate_int_field(
            data, "playwright_timeout_seconds", 5, 60, defaults.playwright_timeout_seconds
        )
        pw_auto_click = self._validate_bool_field(
            data, "playwright_auto_click_play", defaults.playwright_auto_click_play
        )
        streams = self._validate_streams(data.get("streams"))

        return AppConfig(
            max_concurrent_streams=max_cs,
            chunk_duration_seconds=chunk_ds,
            whisper_model=whisper_m,
            output_base_dir=output_dir,
            playwright_timeout_seconds=pw_timeout,
            playwright_auto_click_play=pw_auto_click,
            streams=streams,
        )

    # -- field-level validators --------------------------------------------------

    @staticmethod
    def _validate_int_field(
        data: dict, key: str, lo: int, hi: int, default: int
    ) -> int:
        """Validate an integer config field within [lo, hi]."""
        val = data.get(key)
        if val is None:
            return default
        # bool is a subclass of int in Python — reject it explicitly
        if not isinstance(val, int) or isinstance(val, bool):
            logger.warning(
                "config.yaml: '%s' is not an integer — using default %d", key, default
            )
            return default
        if val < lo or val > hi:
            logger.warning(
                "config.yaml: '%s' value %d outside range [%d, %d] — using default %d",
                key,
                val,
                lo,
                hi,
                default,
            )
            return default
        return val

    @staticmethod
    def _validate_str_field(
        data: dict, key: str, max_len: int, default: str
    ) -> str:
        """Validate a non-empty string config field within max_len."""
        val = data.get(key)
        if val is None:
            return default
        if not isinstance(val, str) or not val.strip():
            logger.warning(
                "config.yaml: '%s' is empty or not a string — using default", key
            )
            return default
        if len(val) > max_len:
            logger.warning(
                "config.yaml: '%s' exceeds %d characters — using default", key, max_len
            )
            return default
        return val

    @staticmethod
    def _validate_bool_field(
        data: dict, key: str, default: bool
    ) -> bool:
        """Validate a boolean config field."""
        val = data.get(key)
        if val is None:
            return default
        if not isinstance(val, bool):
            logger.warning(
                "config.yaml: '%s' is not a boolean — using default %s", key, default
            )
            return default
        return val

    def _validate_streams(self, raw: object) -> list[StreamSource]:
        """Validate the streams list, excluding invalid entries with logged warnings."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            logger.warning("config.yaml: 'streams' is not a list — using empty list")
            return []

        streams: list[StreamSource] = []
        for idx, entry in enumerate(raw):
            if len(streams) >= 50:
                logger.warning(
                    "config.yaml: streams list capped at 50 entries — remaining ignored"
                )
                break
            stream = self._validate_stream_entry(entry, idx)
            if stream is not None:
                streams.append(stream)
        return streams

    @staticmethod
    def _validate_stream_entry(entry: object, index: int) -> Optional[StreamSource]:
        """Validate a single stream entry dict. Returns None for invalid entries."""
        if not isinstance(entry, dict):
            logger.warning(
                "config.yaml: stream entry #%d is not a mapping — skipped", index
            )
            return None

        sid = entry.get("id")
        brand = entry.get("brand")
        url = entry.get("url")

        # id: non-empty string, max 64 chars
        if not isinstance(sid, str) or not sid.strip():
            logger.warning(
                "config.yaml: stream entry #%d has missing/empty 'id' — skipped", index
            )
            return None
        if len(sid) > 64:
            logger.warning(
                "config.yaml: stream entry #%d 'id' exceeds 64 chars — skipped", index
            )
            return None

        # brand: non-empty string, max 128 chars
        if not isinstance(brand, str) or not brand.strip():
            logger.warning(
                "config.yaml: stream entry #%d has missing/empty 'brand' — skipped",
                index,
            )
            return None
        if len(brand) > 128:
            logger.warning(
                "config.yaml: stream entry #%d 'brand' exceeds 128 chars — skipped",
                index,
            )
            return None

        # url: non-empty string, max 2048 chars
        if not isinstance(url, str) or not url.strip():
            logger.warning(
                "config.yaml: stream entry #%d has missing/empty 'url' — skipped",
                index,
            )
            return None
        if len(url) > 2048:
            logger.warning(
                "config.yaml: stream entry #%d 'url' exceeds 2048 chars — skipped",
                index,
            )
            return None

        return StreamSource(id=sid.strip(), brand=brand.strip(), url=url.strip())

    # -- CRUD input validators ---------------------------------------------------

    @staticmethod
    def _validate_brand(brand: str, max_length: int) -> None:
        """Validate a brand name for add/update operations."""
        if not isinstance(brand, str) or not brand.strip():
            raise ValueError("Brand name must be non-empty and not whitespace-only")
        if len(brand.strip()) > max_length:
            raise ValueError(f"Brand name exceeds {max_length} characters")

    @staticmethod
    def _validate_url(url: str, max_length: int = 2048) -> None:
        """Validate URL syntax for add/update operations."""
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL must be non-empty and not whitespace-only")
        stripped = url.strip()
        if len(stripped) > max_length:
            raise ValueError(f"URL exceeds {max_length} characters")
        parsed = urlparse(stripped)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"URL is malformed (missing scheme or host): {stripped}"
            )

    # -- file I/O ----------------------------------------------------------------

    def _write_config(self, config: AppConfig) -> None:
        """Serialize and write an AppConfig to config.yaml."""
        data = {
            "max_concurrent_streams": config.max_concurrent_streams,
            "chunk_duration_seconds": config.chunk_duration_seconds,
            "whisper_model": config.whisper_model,
            "output_base_dir": config.output_base_dir,
            "playwright_timeout_seconds": config.playwright_timeout_seconds,
            "playwright_auto_click_play": config.playwright_auto_click_play,
            "streams": [
                {"id": s.id, "brand": s.brand, "url": s.url}
                for s in config.streams
            ],
        }
        self._config_path.write_text(
            yaml.dump(
                data,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
