from __future__ import annotations

from pathlib import Path

import pytest

from claudemonitor import config
from claudemonitor.config import PollingConfig, TaskbarConfig


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point %APPDATA% at a throwaway directory and yield the config location."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path / "claudemonitor" / "config.toml"


def _write_config(path: Path, text: str) -> None:
    """Create the config directory and place hand-authored TOML inside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_default_polling_interval_is_one_minute():
    assert PollingConfig().interval_seconds == 60


def test_taskbar_display_is_enabled_by_default():
    assert TaskbarConfig().enabled is True


def test_taskbar_visibility_is_persisted_in_existing_config(config_path):
    _write_config(config_path, "[polling]\ninterval_seconds = 30\n")

    config.save_taskbar_enabled(False)

    assert config.load_config().taskbar.enabled is False
    assert config.load_config().polling.interval_seconds == 30

    config.save_taskbar_enabled(True)

    assert config.load_config().taskbar.enabled is True
    assert config.load_config().polling.interval_seconds == 30


def test_taskbar_visibility_updates_dotted_toml_without_duplicate_tables(config_path):
    """Structured TOML editing must understand dotted keys, not append a duplicate table."""
    _write_config(
        config_path,
        "# Keep this user note\ntaskbar.enabled = true\n\n[polling]\ninterval_seconds = 45\n",
    )

    config.save_taskbar_enabled(False)

    saved_text = config_path.read_text(encoding="utf-8")
    assert config.load_config().taskbar.enabled is False
    assert config.load_config().polling.interval_seconds == 45
    assert "# Keep this user note" in saved_text
    assert saved_text.count("enabled = false") == 1


def test_missing_config_is_seeded_with_the_documented_defaults(config_path):
    assert not config_path.exists()

    loaded = config.load_config()

    assert config_path.exists()
    assert "# ClaudeMonitor config" in config_path.read_text(encoding="utf-8")
    assert loaded.polling.interval_seconds == 60
    assert loaded.taskbar.enabled is True


def test_saving_seeds_a_missing_config_before_editing_it(config_path):
    config.save_taskbar_enabled(False)

    assert config.load_config().taskbar.enabled is False
    assert "# ClaudeMonitor config" in config_path.read_text(encoding="utf-8")


def test_malformed_config_falls_back_to_defaults_instead_of_crashing(config_path):
    """A hand-edited typo must not take the whole app down at startup."""
    _write_config(config_path, "[polling\ninterval_seconds = ??\n")

    loaded = config.load_config()

    assert loaded.polling.interval_seconds == 60
    assert loaded.taskbar.enabled is True


def test_wrongly_typed_values_fall_back_to_that_section_defaults(config_path):
    _write_config(
        config_path,
        '[polling]\ninterval_seconds = "soon"\n\n[taskbar]\nenabled = false\n',
    )

    loaded = config.load_config()

    assert loaded.polling.interval_seconds == 60
    # An unrelated bad section must not discard the user's valid settings.
    assert loaded.taskbar.enabled is False


def test_saving_never_leaves_a_truncated_config_behind(config_path):
    """The write is atomic, so an interrupted save cannot corrupt the file."""
    _write_config(config_path, "[polling]\ninterval_seconds = 30\n")
    original_replace = config.os.replace

    def fail_before_replacing(source, destination):
        raise OSError("simulated crash during save")

    config.os.replace = fail_before_replacing
    try:
        with pytest.raises(OSError):
            config.save_taskbar_enabled(False)
    finally:
        config.os.replace = original_replace

    assert config_path.read_text(encoding="utf-8") == "[polling]\ninterval_seconds = 30\n"
