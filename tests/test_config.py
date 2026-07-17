from __future__ import annotations

from pathlib import Path

from claudemonitor import config
from claudemonitor.config import PollingConfig, TaskbarConfig


def test_default_polling_interval_is_one_minute():
    assert PollingConfig().interval_seconds == 60


def test_taskbar_display_is_enabled_by_default():
    assert TaskbarConfig().enabled is True


def test_taskbar_visibility_is_persisted_in_existing_config(monkeypatch):
    appdata = Path.cwd() / ".test-config-appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    path = appdata / "claudemonitor" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text("[polling]\ninterval_seconds = 30\n", encoding="utf-8")

        config.save_taskbar_enabled(False)

        assert config.load_config().taskbar.enabled is False
        assert config.load_config().polling.interval_seconds == 30

        config.save_taskbar_enabled(True)

        assert config.load_config().taskbar.enabled is True
        assert config.load_config().polling.interval_seconds == 30
    finally:
        if path.exists():
            path.unlink()
        if path.parent.exists():
            path.parent.rmdir()
        if appdata.exists():
            appdata.rmdir()
