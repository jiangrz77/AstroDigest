"""Keep user state discoverable when moving to the Astro Digest brand."""

import os
from pathlib import Path
from unittest import mock

import pytest

from src import paths


@pytest.fixture
def support_dirs(tmp_path):
    current = tmp_path / "AstroDigest"
    legacy = tmp_path / "AstroPaperDigest"
    with mock.patch.object(paths, "_APP_SUPPORT_DIR", current), \
            mock.patch.object(paths, "_LEGACY_APP_SUPPORT_DIR", legacy), \
            mock.patch.dict(os.environ, {"ASTRODIGEST_DATA_DIR": "", "APD_DATA_DIR": ""}):
        yield current, legacy


def test_fresh_install_uses_new_data_directory(support_dirs):
    current, _ = support_dirs
    with mock.patch("sys.frozen", True, create=True):
        assert paths.data_dir() == current
        assert paths.app_support_dir() == current
        assert current.is_dir()


def test_existing_settings_and_history_remain_in_legacy_directory(support_dirs):
    current, legacy = support_dirs
    legacy.mkdir()
    settings = legacy / "preferences.json"
    settings.write_text('{"language": "zh"}')
    # Even if a new empty folder was created, legacy app instances share state.
    current.mkdir()
    with mock.patch("sys.frozen", True, create=True):
        assert paths.data_dir() == legacy
        assert paths.app_support_dir() == legacy
    assert settings.read_text() == '{"language": "zh"}'


def test_new_override_takes_precedence_over_legacy_override(tmp_path):
    current = tmp_path / "new"
    legacy = tmp_path / "old"
    with mock.patch.dict(os.environ, {"ASTRODIGEST_DATA_DIR": str(current), "APD_DATA_DIR": str(legacy)}):
        assert paths.app_support_dir() == current


def test_legacy_override_still_works(tmp_path):
    with mock.patch.dict(os.environ, {"ASTRODIGEST_DATA_DIR": "", "APD_DATA_DIR": str(tmp_path)}):
        assert paths.app_support_dir() == tmp_path


def test_relative_new_override_is_rejected():
    with mock.patch.dict(os.environ, {"ASTRODIGEST_DATA_DIR": "relative"}):
        with pytest.raises(ValueError, match="ASTRODIGEST_DATA_DIR must be an absolute path"):
            paths.app_support_dir()
