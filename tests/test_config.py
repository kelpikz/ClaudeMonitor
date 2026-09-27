from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claudemonitor import config
from claudemonitor.config import (
    CLAUDE_SETTINGS,
    CODEX_SETTINGS,
    ClaudeConfig,
    CodexConfig,
    PollingConfig,
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


def test_taskbar_visibility_is_persisted_in_existing_config(config_path):
    _write_config(config_path, "[polling]\ninterval_seconds = 30\n")

    config.TASKBAR_ENABLED.save(False)

    assert config.load_config().taskbar.enabled is False
    assert config.load_config().polling.interval_seconds == 30

    config.TASKBAR_ENABLED.save(True)

    assert config.load_config().taskbar.enabled is True
    assert config.load_config().polling.interval_seconds == 30


def test_taskbar_visibility_updates_dotted_toml_without_duplicate_tables(config_path):
    """Structured TOML editing must understand dotted keys, not append a duplicate table."""
    _write_config(
        config_path,
        "# Keep this user note\ntaskbar.enabled = true\n\n[polling]\ninterval_seconds = 45\n",
    )

    config.TASKBAR_ENABLED.save(False)

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
    config.TASKBAR_ENABLED.save(False)

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


def test_saving_never_leaves_a_truncated_config_behind(config_path, monkeypatch):
    """The write is atomic, so an interrupted save cannot corrupt the file."""
    _write_config(config_path, "[polling]\ninterval_seconds = 30\n")

    def fail_before_replacing(source, destination):
        raise OSError("simulated crash during save")

    monkeypatch.setattr(config.os, "replace", fail_before_replacing)

    config.TASKBAR_ENABLED.save(False)

    assert config_path.read_text(encoding="utf-8") == "[polling]\ninterval_seconds = 30\n"


def test_a_save_that_cannot_be_written_is_logged_rather_than_raised(
    config_path, monkeypatch, caplog
):
    """Every caller runs on a UI thread and has already changed the running app.

    A file that cannot be written must not undo that, nor take the thread down.
    """

    def unwritable(source, destination):
        raise OSError("the disk is full")

    monkeypatch.setattr(config.os, "replace", unwritable)

    with caplog.at_level(logging.ERROR):
        config.TASKBAR_ENABLED.save(False)

    assert "taskbar" in caplog.text and "enabled" in caplog.text


# ===========================================================================
# One section per provider: tracking, both refresh switches, cooldown, model.
# ===========================================================================


class TestProviderSectionDefaults:
    """Both providers are tracked and refreshed unless the user says otherwise."""

    @pytest.mark.parametrize("section", [ClaudeConfig, CodexConfig])
    def test_tracking_and_both_refreshes_are_on(self, section):
        loaded = section()

        assert loaded.enabled is True
        assert loaded.renew_token is True
        assert loaded.wake_session is True

    @pytest.mark.parametrize("section", [ClaudeConfig, CodexConfig])
    def test_the_cooldown_is_fifteen_minutes(self, section):
        assert section().cooldown_seconds == 900

    def test_claude_asks_its_cheapest_model(self):
        assert ClaudeConfig().model == "haiku"

    def test_codex_keeps_its_own_model_but_asks_for_little_reasoning(self):
        # A user's Codex config may ask for xhigh effort, which a one-word
        # nudge has no use for.
        assert CodexConfig().model == ""
        assert CodexConfig().effort == "low"


class TestReadingAProviderSection:
    def test_claude_can_be_turned_off_in_the_file(self, config_path):
        _write_config(config_path, "[claude]\nenabled = false\n")

        assert config.load_config().claude.enabled is False

    def test_codex_can_be_turned_off_in_the_file(self, config_path):
        _write_config(config_path, "[codex]\nenabled = false\n")

        assert config.load_config().codex.enabled is False

    def test_every_refresh_setting_is_read(self, config_path):
        _write_config(
            config_path,
            "[codex]\nrenew_token = false\nwake_session = false\n"
            'cooldown_seconds = 60\nmodel = "gpt-5.5"\neffort = "high"\n',
        )

        codex = config.load_config().codex

        assert (codex.renew_token, codex.wake_session) == (False, False)
        assert codex.cooldown_seconds == 60
        assert (codex.model, codex.effort) == ("gpt-5.5", "high")

    def test_one_provider_s_section_does_not_leak_into_the_other(self, config_path):
        _write_config(config_path, "[codex]\nwake_session = false\n")

        assert config.load_config().claude.wake_session is True

    def test_a_wrongly_typed_section_falls_back_to_its_defaults(self, config_path):
        _write_config(config_path, '[codex]\nenabled = "yes please"\n')

        assert config.load_config().codex.enabled is True

    def test_the_seeded_file_documents_both_sections(self, config_path):
        config.load_config()

        text = config_path.read_text(encoding="utf-8")
        assert "[claude]" in text
        assert "[codex]" in text
        assert "[session_refresh]" not in text


class TestTheOldSharedRefreshSection:
    """A config written before the split had one [session_refresh] for both.

    Its values become each provider's starting point, so an upgrade keeps what
    the user chose; a provider section that says otherwise wins.
    """

    def test_switching_refresh_off_there_switches_both_reasons_off(self, config_path):
        _write_config(config_path, "[session_refresh]\nenabled = false\n")

        loaded = config.load_config()

        for provider in (loaded.claude, loaded.codex):
            assert provider.renew_token is False
            assert provider.wake_session is False

    def test_its_cooldown_reaches_both_providers(self, config_path):
        _write_config(config_path, "[session_refresh]\ncooldown_seconds = 120\n")

        loaded = config.load_config()

        assert loaded.claude.cooldown_seconds == 120
        assert loaded.codex.cooldown_seconds == 120

    def test_a_provider_section_overrides_it(self, config_path):
        _write_config(
            config_path,
            "[session_refresh]\nenabled = false\n\n[codex]\nwake_session = true\n",
        )

        codex = config.load_config().codex

        assert codex.wake_session is True
        assert codex.renew_token is False

    def test_a_wrongly_typed_old_value_is_ignored_rather_than_costing_the_section(
        self, config_path
    ):
        _write_config(
            config_path,
            '[session_refresh]\ncooldown_seconds = "soon"\n\n[codex]\nenabled = false\n',
        )

        codex = config.load_config().codex

        assert codex.enabled is False
        assert codex.cooldown_seconds == 900


class TestSavingAProviderSetting:
    def test_a_switch_is_persisted_without_touching_other_settings(self, config_path):
        _write_config(
            config_path,
            "[polling]\ninterval_seconds = 45\n\n[codex]\nenabled = true\n",
        )

        CODEX_SETTINGS.tracking.save(False)

        reloaded = config.load_config()
        assert reloaded.codex.enabled is False
        assert reloaded.polling.interval_seconds == 45

    def test_a_missing_section_is_seeded(self, config_path):
        _write_config(config_path, "# Keep this user note\n[polling]\ninterval_seconds = 30\n")

        CLAUDE_SETTINGS.wake_session.save(False)

        assert config.load_config().claude.wake_session is False
        assert "Keep this user note" in config_path.read_text(encoding="utf-8")

    def test_the_model_is_saved_as_text(self, config_path):
        CODEX_SETTINGS.model.save("gpt-5.5")

        assert config.load_config().codex.model == "gpt-5.5"


class TestEveryProviderHasTheSameSettings:
    @pytest.mark.parametrize(
        ("settings", "section"), [(CLAUDE_SETTINGS, "claude"), (CODEX_SETTINGS, "codex")]
    )
    def test_each_setting_lives_in_the_provider_s_own_section(self, settings, section):
        assert {setting.section for setting in settings.every()} == {section}

    def test_each_is_offered_for_writing(self):
        for settings in (CLAUDE_SETTINGS, CODEX_SETTINGS):
            for setting in settings.every():
                assert setting in config.EVERY_SETTING


class TestSavingTheNumericSettings:
    """The settings window now writes what used to be file-only, so each of
    these values needs a writer that leaves every other setting alone."""

    def test_the_poll_interval_is_persisted(self, config_path):
        _write_config(config_path, "[polling]\ninterval_seconds = 60\n")

        config.POLL_INTERVAL.save(120)

        assert config.load_config().polling.interval_seconds == 120

    def test_the_amber_threshold_is_persisted(self, config_path):
        _write_config(config_path, "[thresholds]\namber_below = 50\nred_below = 20\n")

        config.AMBER_THRESHOLD.save(40)

        loaded = config.load_config()
        assert loaded.thresholds.amber_below == 40
        assert loaded.thresholds.red_below == 20

    def test_the_red_threshold_is_persisted(self, config_path):
        _write_config(config_path, "[thresholds]\namber_below = 50\nred_below = 20\n")

        config.RED_THRESHOLD.save(10)

        loaded = config.load_config()
        assert loaded.thresholds.red_below == 10
        assert loaded.thresholds.amber_below == 50

    def test_a_provider_cooldown_is_persisted(self, config_path):
        _write_config(config_path, "[codex]\nenabled = true\ncooldown_seconds = 900\n")

        CODEX_SETTINGS.cooldown.save(300)

        loaded = config.load_config()
        assert loaded.codex.cooldown_seconds == 300
        assert loaded.codex.enabled is True

    def test_a_numeric_write_seeds_a_missing_section(self, config_path):
        _write_config(config_path, "# Keep this user note\n[polling]\ninterval_seconds = 45\n")

        config.AMBER_THRESHOLD.save(35)

        assert config.load_config().thresholds.amber_below == 35
        assert "Keep this user note" in config_path.read_text(encoding="utf-8")

    def test_a_numeric_write_seeds_a_missing_config_file(self, config_path):
        config.POLL_INTERVAL.save(90)

        assert config.load_config().polling.interval_seconds == 90


class TestConfigSetting:
    """One setting names its section and key once, for both the file and the model.

    The section and the key used to be spelled twice — once in a ``save_*``
    wrapper in this module, once as a string in ``main`` — for every setting
    the settings window can write.
    """

    def test_every_setting_names_a_section_the_model_actually_has(self):
        loaded = config.Config()

        for setting in config.EVERY_SETTING:
            assert hasattr(loaded, setting.section), setting

    def test_every_setting_names_a_key_that_section_actually_has(self):
        loaded = config.Config()

        for setting in config.EVERY_SETTING:
            assert hasattr(getattr(loaded, setting.section), setting.key), setting

    def test_reading_returns_what_the_loaded_config_holds(self):
        loaded = config.Config()
        loaded.thresholds.amber_below = 35

        assert config.AMBER_THRESHOLD.read(loaded) == 35

    def test_writing_changes_the_running_config(self):
        loaded = config.Config()

        config.AMBER_THRESHOLD.write(loaded, 42)

        assert loaded.thresholds.amber_below == 42

    def test_writing_does_not_touch_the_file(self, config_path):
        _write_config(config_path, "[thresholds]\namber_below = 50\n")

        config.AMBER_THRESHOLD.write(config.Config(), 42)

        assert config.load_config().thresholds.amber_below == 50

    def test_saving_and_reloading_round_trips(self, config_path):
        for setting, value in (
            (config.POLL_INTERVAL, 120),
            (config.AMBER_THRESHOLD, 40),
            (config.RED_THRESHOLD, 15),
            (config.TASKBAR_ENABLED, False),
            (CLAUDE_SETTINGS.tracking, False),
            (CLAUDE_SETTINGS.renew_token, False),
            (CLAUDE_SETTINGS.wake_session, False),
            (CLAUDE_SETTINGS.cooldown, 300),
            (CLAUDE_SETTINGS.model, "sonnet"),
            (CLAUDE_SETTINGS.effort, "low"),
            (CODEX_SETTINGS.tracking, False),
            (CODEX_SETTINGS.cooldown, 600),
            (CODEX_SETTINGS.model, "gpt-5.5"),
            (CODEX_SETTINGS.effort, "high"),
        ):
            setting.save(value)

            assert setting.read(config.load_config()) == value, setting
