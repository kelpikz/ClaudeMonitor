from __future__ import annotations

from pathlib import Path

import pytest

from claudemonitor import config
from claudemonitor.config import (
    CodexConfig,
    PollingConfig,
    SessionRefreshConfig,
    TaskbarConfig,
)


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


def test_session_refresh_is_enabled_with_a_fifteen_minute_cooldown_by_default():
    assert SessionRefreshConfig().enabled is True
    assert SessionRefreshConfig().cooldown_seconds == 900


def test_session_refresh_can_be_turned_off_in_the_config_file(config_path):
    _write_config(config_path, "[session_refresh]\nenabled = false\ncooldown_seconds = 60\n")

    loaded = config.load_config()

    assert loaded.session_refresh.enabled is False
    assert loaded.session_refresh.cooldown_seconds == 60


def test_session_refresh_toggle_is_persisted_without_touching_other_settings(config_path):
    _write_config(
        config_path,
        "[polling]\ninterval_seconds = 30\n\n[session_refresh]\ncooldown_seconds = 120\n",
    )

    config.save_session_refresh_enabled(False)

    loaded = config.load_config()
    assert loaded.session_refresh.enabled is False
    assert loaded.session_refresh.cooldown_seconds == 120
    assert loaded.polling.interval_seconds == 30

    config.save_session_refresh_enabled(True)

    assert config.load_config().session_refresh.enabled is True


def test_session_refresh_toggle_seeds_the_section_when_it_is_absent(config_path):
    _write_config(config_path, "# Keep this user note\n[polling]\ninterval_seconds = 45\n")

    config.save_session_refresh_enabled(False)

    saved_text = config_path.read_text(encoding="utf-8")
    assert config.load_config().session_refresh.enabled is False
    assert "# Keep this user note" in saved_text


def test_seeded_config_documents_the_session_refresh_section(config_path):
    loaded = config.load_config()

    assert "[session_refresh]" in config_path.read_text(encoding="utf-8")
    assert loaded.session_refresh.enabled is True


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


# ===========================================================================
# Codex tracking — on by default, switchable from the tray or the file.
# ===========================================================================


def test_codex_tracking_is_enabled_by_default():
    assert CodexConfig().enabled is True


def test_codex_tracking_can_be_turned_off_in_the_config_file(config_path):
    _write_config(config_path, "[codex]\nenabled = false\n")

    assert config.load_config().codex.enabled is False


def test_codex_toggle_is_persisted_without_touching_other_settings(config_path):
    _write_config(
        config_path,
        "[polling]\ninterval_seconds = 45\n\n[codex]\nenabled = true\n",
    )

    config.save_codex_enabled(False)

    reloaded = config.load_config()
    assert reloaded.codex.enabled is False
    assert reloaded.polling.interval_seconds == 45


def test_codex_toggle_seeds_the_section_when_it_is_absent(config_path):
    _write_config(config_path, "[polling]\ninterval_seconds = 30\n")

    config.save_codex_enabled(False)

    assert config.load_config().codex.enabled is False


def test_seeded_config_documents_the_codex_section(config_path):
    config.load_config()

    assert "[codex]" in config_path.read_text(encoding="utf-8")


def test_a_wrongly_typed_codex_section_falls_back_to_the_default(config_path):
    _write_config(config_path, '[codex]\nenabled = "yes please"\n')

    assert config.load_config().codex.enabled is True


class TestSavingTheNumericSettings:
    """The settings window now writes what used to be file-only, so each of
    these values needs a writer that leaves every other setting alone."""

    def test_the_poll_interval_is_persisted(self, config_path):
        _write_config(config_path, "[polling]\ninterval_seconds = 60\n")

        config.save_poll_interval_seconds(120)

        assert config.load_config().polling.interval_seconds == 120

    def test_the_amber_threshold_is_persisted(self, config_path):
        _write_config(config_path, "[thresholds]\namber_below = 50\nred_below = 20\n")

        config.save_amber_threshold(40)

        loaded = config.load_config()
        assert loaded.thresholds.amber_below == 40
        assert loaded.thresholds.red_below == 20

    def test_the_red_threshold_is_persisted(self, config_path):
        _write_config(config_path, "[thresholds]\namber_below = 50\nred_below = 20\n")

        config.save_red_threshold(10)

        loaded = config.load_config()
        assert loaded.thresholds.red_below == 10
        assert loaded.thresholds.amber_below == 50

    def test_the_refresh_cooldown_is_persisted(self, config_path):
        _write_config(
            config_path, "[session_refresh]\nenabled = true\ncooldown_seconds = 900\n"
        )

        config.save_session_refresh_cooldown(300)

        loaded = config.load_config()
        assert loaded.session_refresh.cooldown_seconds == 300
        assert loaded.session_refresh.enabled is True

    def test_a_numeric_write_seeds_a_missing_section(self, config_path):
        _write_config(config_path, "# Keep this user note\n[polling]\ninterval_seconds = 45\n")

        config.save_amber_threshold(35)

        assert config.load_config().thresholds.amber_below == 35
        assert "Keep this user note" in config_path.read_text(encoding="utf-8")

    def test_a_numeric_write_seeds_a_missing_config_file(self, config_path):
        config.save_poll_interval_seconds(90)

        assert config.load_config().polling.interval_seconds == 90
