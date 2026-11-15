"""Unit tests for stream_manager helper functions."""

import os
from datetime import datetime

from stream_manager import (
    compute_backoff_delay,
    format_transcript_line,
    generate_session_path,
)


class TestGenerateSessionPath:
    """Tests for generate_session_path()."""

    def test_basic_path_structure(self, tmp_path):
        dt = datetime(2025, 3, 15, 14, 30, 45)
        path = generate_session_path(str(tmp_path), "BrandX", dt)

        expected = os.path.join(
            str(tmp_path), "BrandX", "2025-03-15", "BrandX_2025-03-15_14-30-45.txt"
        )
        assert path == expected

    def test_creates_parent_directories(self, tmp_path):
        dt = datetime(2025, 1, 1, 0, 0, 0)
        path = generate_session_path(str(tmp_path), "NewBrand", dt)

        assert os.path.isdir(os.path.dirname(path))

    def test_collision_appends_numeric_suffix(self, tmp_path):
        dt = datetime(2025, 6, 10, 8, 5, 3)
        # Create the first file so the next call collides
        first = generate_session_path(str(tmp_path), "Radio", dt)
        with open(first, "w") as f:
            f.write("first")

        second = generate_session_path(str(tmp_path), "Radio", dt)
        assert second.endswith("Radio_2025-06-10_08-05-03_1.txt")
        assert second != first

    def test_multiple_collisions(self, tmp_path):
        dt = datetime(2025, 12, 31, 23, 59, 59)
        brand = "Test"

        # Create base and _1 files
        p0 = generate_session_path(str(tmp_path), brand, dt)
        with open(p0, "w") as f:
            f.write("0")

        p1 = generate_session_path(str(tmp_path), brand, dt)
        with open(p1, "w") as f:
            f.write("1")

        p2 = generate_session_path(str(tmp_path), brand, dt)
        assert p2.endswith("Test_2025-12-31_23-59-59_2.txt")

    def test_zero_padded_date_and_time(self, tmp_path):
        dt = datetime(2025, 1, 2, 3, 4, 5)
        path = generate_session_path(str(tmp_path), "B", dt)
        assert "2025-01-02" in path
        assert "03-04-05" in path

    def test_same_day_shares_date_directory(self, tmp_path):
        dt1 = datetime(2025, 7, 4, 10, 0, 0)
        dt2 = datetime(2025, 7, 4, 22, 30, 0)

        p1 = generate_session_path(str(tmp_path), "Ch", dt1)
        p2 = generate_session_path(str(tmp_path), "Ch", dt2)

        assert os.path.dirname(p1) == os.path.dirname(p2)

    def test_different_day_uses_different_directory(self, tmp_path):
        dt1 = datetime(2025, 7, 4, 23, 59, 59)
        dt2 = datetime(2025, 7, 5, 0, 0, 0)

        p1 = generate_session_path(str(tmp_path), "Ch", dt1)
        p2 = generate_session_path(str(tmp_path), "Ch", dt2)

        assert os.path.dirname(p1) != os.path.dirname(p2)


class TestFormatTranscriptLine:
    """Tests for format_transcript_line()."""

    def test_basic_format(self):
        t = datetime(2025, 1, 1, 14, 32, 5)
        result = format_transcript_line(t, "Hello world")
        assert result == "[14:32:05] Hello world"

    def test_zero_padded_components(self):
        t = datetime(2025, 1, 1, 0, 0, 0)
        result = format_transcript_line(t, "midnight")
        assert result == "[00:00:00] midnight"

    def test_preserves_text_exactly(self):
        t = datetime(2025, 1, 1, 12, 0, 0)
        text = "  Leading spaces and trailing  "
        result = format_transcript_line(t, text)
        assert result == f"[12:00:00] {text}"

    def test_max_time_values(self):
        t = datetime(2025, 1, 1, 23, 59, 59)
        result = format_transcript_line(t, "end of day")
        assert result == "[23:59:59] end of day"


class TestComputeBackoffDelay:
    """Tests for compute_backoff_delay()."""

    def test_attempt_sequence(self):
        assert compute_backoff_delay(1) == 2
        assert compute_backoff_delay(2) == 4
        assert compute_backoff_delay(3) == 8
        assert compute_backoff_delay(4) == 16
        assert compute_backoff_delay(5) == 32

    def test_capped_at_32(self):
        assert compute_backoff_delay(6) == 32
        assert compute_backoff_delay(10) == 32

    def test_attempt_zero(self):
        # 2^0 = 1, below cap
        assert compute_backoff_delay(0) == 1
