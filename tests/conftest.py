"""Shared fixtures for ConfigManager and related tests."""

import os
import tempfile

import pytest


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory path as a string."""
    return str(tmp_path)


@pytest.fixture()
def config_path(tmp_path):
    """Return a path for a config.yaml inside a temp directory."""
    return str(tmp_path / "config.yaml")
