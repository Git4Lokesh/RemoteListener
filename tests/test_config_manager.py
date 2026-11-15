"""Unit tests for ConfigManager — covers core load/validate/CRUD behaviour."""

from __future__ import annotations

import os
from typing import Union

import pytest
import yaml

from config_manager import ConfigManager
from models import AppConfig, StreamSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(path: str, data: dict | str) -> None:
    """Write raw YAML (dict or string) to *path*."""
    content = data if isinstance(data, str) else yaml.dump(data, sort_keys=False)
    with open(path, "w") as f:
        f.write(content)


def _read_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Default creation
# ---------------------------------------------------------------------------

class TestDefaultCreation:
    """When config.yaml does not exist, ConfigManager creates one with defaults."""

    def test_creates_default_file(self, config_path):
        cm = ConfigManager(config_path)
        assert os.path.exists(config_path)
        data = _read_yaml(config_path)
        assert data["max_concurrent_streams"] == 5
        assert data["chunk_duration_seconds"] == 10
        assert data["whisper_model"] == "base.en"
        assert data["streams"] == []

    def test_config_matches_defaults(self, config_path):
        cm = ConfigManager(config_path)
        cfg = cm.config
        assert cfg.max_concurrent_streams == 5
        assert cfg.chunk_duration_seconds == 10
        assert cfg.whisper_model == "base.en"
        assert cfg.streams == []


# ---------------------------------------------------------------------------
# Loading valid config
# ---------------------------------------------------------------------------

class TestLoadValidConfig:

    def test_loads_custom_scalars(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 10,
            "chunk_duration_seconds": 60,
            "whisper_model": "large-v2",
            "output_base_dir": "/tmp/out",
            "streams": [],
        })
        cm = ConfigManager(config_path)
        assert cm.config.max_concurrent_streams == 10
        assert cm.config.chunk_duration_seconds == 60
        assert cm.config.whisper_model == "large-v2"
        assert cm.config.output_base_dir == "/tmp/out"

    def test_loads_streams(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": [
                {"id": "abc1", "brand": "TestBrand", "url": "https://example.com/live"},
            ],
        })
        cm = ConfigManager(config_path)
        assert len(cm.config.streams) == 1
        s = cm.config.streams[0]
        assert s.id == "abc1"
        assert s.brand == "TestBrand"
        assert s.url == "https://example.com/live"


# ---------------------------------------------------------------------------
# Fallback to defaults for invalid values
# ---------------------------------------------------------------------------

class TestInvalidScalarFallback:

    def test_out_of_range_int_uses_default(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 999,
            "chunk_duration_seconds": 1,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": [],
        })
        cm = ConfigManager(config_path)
        assert cm.config.max_concurrent_streams == 5  # default
        assert cm.config.chunk_duration_seconds == 10  # default

    def test_wrong_type_uses_default(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": "not_int",
            "chunk_duration_seconds": True,
            "whisper_model": 42,
            "output_base_dir": "",
            "streams": [],
        })
        cm = ConfigManager(config_path)
        defaults = AppConfig()
        assert cm.config.max_concurrent_streams == defaults.max_concurrent_streams
        assert cm.config.chunk_duration_seconds == defaults.chunk_duration_seconds
        assert cm.config.whisper_model == defaults.whisper_model
        assert cm.config.output_base_dir == defaults.output_base_dir

    def test_empty_string_fields_use_default(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "   ",
            "output_base_dir": "",
            "streams": [],
        })
        cm = ConfigManager(config_path)
        defaults = AppConfig()
        assert cm.config.whisper_model == defaults.whisper_model
        assert cm.config.output_base_dir == defaults.output_base_dir


# ---------------------------------------------------------------------------
# Invalid stream entries excluded
# ---------------------------------------------------------------------------

class TestInvalidStreamEntries:

    def test_missing_id_excluded(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": [
                {"brand": "NoBrand", "url": "https://example.com"},
            ],
        })
        cm = ConfigManager(config_path)
        assert len(cm.config.streams) == 0

    def test_empty_brand_excluded(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": [
                {"id": "x1", "brand": "", "url": "https://example.com"},
            ],
        })
        cm = ConfigManager(config_path)
        assert len(cm.config.streams) == 0

    def test_non_dict_entry_excluded(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": ["not_a_dict"],
        })
        cm = ConfigManager(config_path)
        assert len(cm.config.streams) == 0

    def test_valid_entries_kept_alongside_invalid(self, config_path):
        _write_yaml(config_path, {
            "max_concurrent_streams": 5,
            "chunk_duration_seconds": 10,
            "whisper_model": "base.en",
            "output_base_dir": "/tmp/out",
            "streams": [
                {"id": "ok1", "brand": "Good", "url": "https://example.com/1"},
                {"id": "", "brand": "Bad", "url": "https://example.com/2"},
                {"id": "ok2", "brand": "Also Good", "url": "https://example.com/3"},
            ],
        })
        cm = ConfigManager(config_path)
        assert len(cm.config.streams) == 2
        assert cm.config.streams[0].id == "ok1"
        assert cm.config.streams[1].id == "ok2"


# ---------------------------------------------------------------------------
# Syntactically invalid YAML
# ---------------------------------------------------------------------------

class TestInvalidYaml:

    def test_invalid_yaml_uses_all_defaults(self, config_path):
        _write_yaml(config_path, "{{{{ not valid yaml ::::")
        cm = ConfigManager(config_path)
        defaults = AppConfig()
        assert cm.config.max_concurrent_streams == defaults.max_concurrent_streams
        assert cm.config.streams == []


# ---------------------------------------------------------------------------
# add_stream
# ---------------------------------------------------------------------------

class TestAddStream:

    def test_add_valid_stream(self, config_path):
        cm = ConfigManager(config_path)
        s = cm.add_stream("TestChannel", "https://example.com/live")
        assert s.brand == "TestChannel"
        assert s.url == "https://example.com/live"
        assert len(s.id) == 8
        assert s in cm.config.streams

    def test_add_stream_persisted_to_file(self, config_path):
        cm = ConfigManager(config_path)
        cm.add_stream("Persisted", "https://example.com/persisted")
        data = _read_yaml(config_path)
        assert len(data["streams"]) == 1
        assert data["streams"][0]["brand"] == "Persisted"

    def test_add_stream_strips_whitespace(self, config_path):
        cm = ConfigManager(config_path)
        s = cm.add_stream("  Padded  ", "  https://example.com/padded  ")
        assert s.brand == "Padded"
        assert s.url == "https://example.com/padded"

    def test_add_duplicate_url_rejected(self, config_path):
        cm = ConfigManager(config_path)
        cm.add_stream("First", "https://example.com/same")
        with pytest.raises(ValueError, match="already configured"):
            cm.add_stream("Second", "https://example.com/same")
        assert len(cm.config.streams) == 1

    def test_add_empty_brand_rejected(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="non-empty"):
            cm.add_stream("", "https://example.com/x")

    def test_add_whitespace_brand_rejected(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="non-empty"):
            cm.add_stream("   ", "https://example.com/x")

    def test_add_brand_exceeds_100_chars(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="100 characters"):
            cm.add_stream("A" * 101, "https://example.com/x")

    def test_add_brand_exactly_100_chars_accepted(self, config_path):
        cm = ConfigManager(config_path)
        s = cm.add_stream("B" * 100, "https://example.com/x")
        assert len(s.brand) == 100

    def test_add_malformed_url_rejected(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="malformed"):
            cm.add_stream("Name", "not_a_url")

    def test_add_url_missing_scheme_rejected(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="malformed"):
            cm.add_stream("Name", "example.com/live")

    def test_add_empty_url_rejected(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(ValueError, match="non-empty"):
            cm.add_stream("Name", "")


# ---------------------------------------------------------------------------
# update_stream
# ---------------------------------------------------------------------------

class TestUpdateStream:

    def _seed_stream(self, cm):
        return cm.add_stream("Original", "https://example.com/original")

    def test_update_brand(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        updated = cm.update_stream(s.id, brand="NewName")
        assert updated.brand == "NewName"
        assert updated.url == "https://example.com/original"

    def test_update_url(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        updated = cm.update_stream(s.id, url="https://new.example.com/live")
        assert updated.url == "https://new.example.com/live"
        assert updated.brand == "Original"

    def test_update_persisted(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        cm.update_stream(s.id, brand="Saved")
        data = _read_yaml(config_path)
        assert data["streams"][0]["brand"] == "Saved"

    def test_update_nonexistent_stream_raises(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(KeyError):
            cm.update_stream("does_not_exist", brand="X")

    def test_update_brand_exceeds_50_chars(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        with pytest.raises(ValueError, match="50 characters"):
            cm.update_stream(s.id, brand="X" * 51)

    def test_update_brand_exactly_50_chars_accepted(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        updated = cm.update_stream(s.id, brand="Y" * 50)
        assert len(updated.brand) == 50

    def test_update_empty_brand_rejected(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        with pytest.raises(ValueError, match="non-empty"):
            cm.update_stream(s.id, brand="")

    def test_update_duplicate_brand_rejected(self, config_path):
        cm = ConfigManager(config_path)
        s1 = cm.add_stream("Alpha", "https://example.com/a")
        s2 = cm.add_stream("Beta", "https://example.com/b")
        with pytest.raises(ValueError, match="already in use"):
            cm.update_stream(s2.id, brand="Alpha")
        # Original values unchanged
        assert cm.get_stream(s2.id).brand == "Beta"

    def test_update_malformed_url_rejected(self, config_path):
        cm = ConfigManager(config_path)
        s = self._seed_stream(cm)
        with pytest.raises(ValueError, match="malformed"):
            cm.update_stream(s.id, url="bad_url")
        assert cm.get_stream(s.id).url == "https://example.com/original"


# ---------------------------------------------------------------------------
# remove_stream
# ---------------------------------------------------------------------------

class TestRemoveStream:

    def test_remove_existing_stream(self, config_path):
        cm = ConfigManager(config_path)
        s = cm.add_stream("ToRemove", "https://example.com/remove")
        cm.remove_stream(s.id)
        assert len(cm.config.streams) == 0

    def test_remove_persisted(self, config_path):
        cm = ConfigManager(config_path)
        s = cm.add_stream("ToRemove", "https://example.com/remove")
        cm.remove_stream(s.id)
        data = _read_yaml(config_path)
        assert len(data["streams"]) == 0

    def test_remove_nonexistent_raises(self, config_path):
        cm = ConfigManager(config_path)
        with pytest.raises(KeyError):
            cm.remove_stream("ghost")


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

class TestReload:

    def test_reload_picks_up_external_changes(self, config_path):
        cm = ConfigManager(config_path)
        # Externally modify the file
        data = _read_yaml(config_path)
        data["max_concurrent_streams"] = 15
        _write_yaml(config_path, data)

        reloaded = cm.reload()
        assert reloaded.max_concurrent_streams == 15
        assert cm.config.max_concurrent_streams == 15


# ---------------------------------------------------------------------------
# save round-trip
# ---------------------------------------------------------------------------

class TestSaveRoundTrip:

    def test_save_and_reload_preserves_config(self, config_path):
        cm = ConfigManager(config_path)
        cm.add_stream("RTrip", "https://example.com/rt")
        cm.save()

        cm2 = ConfigManager(config_path)
        assert len(cm2.config.streams) == 1
        assert cm2.config.streams[0].brand == "RTrip"
