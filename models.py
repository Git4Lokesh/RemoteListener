"""Shared data models for the Multi-Stream Listener & Transcriber application.

Defines core enums, dataclasses for internal use, and Pydantic models for
API request/response serialization.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StreamState(str, Enum):
    """Possible states for a stream lifecycle."""
    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    RECONNECTING = "reconnecting"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Dataclasses (internal domain objects)
# ---------------------------------------------------------------------------

@dataclass
class StreamSource:
    """A configured stream entry with unique ID, brand name, and URL."""
    id: str
    brand: str
    url: str


@dataclass
class AppConfig:
    """Application-wide configuration loaded from config.yaml."""
    max_concurrent_streams: int = 5
    chunk_duration_seconds: int = 10
    whisper_model: str = "base.en"
    output_base_dir: str = ""  # Resolved dynamically at runtime
    playwright_timeout_seconds: int = 15
    playwright_auto_click_play: bool = True
    streams: list[StreamSource] = field(default_factory=list)

    def __post_init__(self):
        """Resolve output_base_dir: TRANSCRIPT_DIR env var > ~/Desktop/transcripts."""
        if not self.output_base_dir:
            import os
            from pathlib import Path as _P
            self.output_base_dir = os.environ.get(
                'TRANSCRIPT_DIR',
                str(_P.home() / 'Desktop' / 'transcripts')
            )


@dataclass
class StreamStatus:
    """Runtime status snapshot for a single stream."""
    id: str
    brand: str
    url: str
    state: StreamState
    error_message: Optional[str] = None
    reconnect_attempt: Optional[int] = None
    has_transcript: bool = False
    extractor_type: Optional[str] = None
    daily_file_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Pydantic models (API request / response schemas)
# ---------------------------------------------------------------------------

class AddStreamRequest(BaseModel):
    """Request body for adding a new stream source."""
    brand: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=2048)


class UpdateStreamRequest(BaseModel):
    """Request body for editing an existing stream source."""
    brand: Optional[str] = Field(None, min_length=1, max_length=50)
    url: Optional[str] = Field(None, min_length=1, max_length=2048)


class StreamStatusResponse(BaseModel):
    """Response model mirroring StreamStatus for API consumers."""
    id: str
    brand: str
    url: str
    state: str
    error_message: Optional[str] = None
    reconnect_attempt: Optional[int] = None
    has_transcript: bool = False
    extractor_type: Optional[str] = None
    daily_file_path: Optional[str] = None


class StartAllResponse(BaseModel):
    """Response model for the start-all endpoint."""
    started: list[str]
    failed: list[dict]
    skipped: list[str]


class ErrorResponse(BaseModel):
    """Standard error response body."""
    detail: str
