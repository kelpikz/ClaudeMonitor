from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from claudemonitor import autostart, main, tray
from claudemonitor.models import DisplayState


class _FakeRegistry:
    """Emulate the small winreg surface used by per-user startup registration."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, value: str | None = None):
        self.value = value
        self.writes: list[str] = []
        self.deletes = 0

    def OpenKey(self, root, path, reserved=0, access=0):
        if self.value is None and access == self.KEY_READ:
            raise FileNotFoundError(path)
        return self

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        return self

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.REG_SZ

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.value = value
        self.writes.append(value)

    def DeleteValue(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        self.value = None
        self.deletes += 1

    def CloseKey(self, key):
        return None


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeRegistry()
    monkeypatch.setattr(autostart, "winreg", registry)
    return registry


class TestStartupCommand:
    def test_packaged_app_registers_the_executable_itself(self):
        command = autostart.startup_command(
            executable=Path(r"C:\Program Files\Claude Monitor\ClaudeMonitor.exe"),
            frozen=True,
        )

        assert command == '"C:\\Program Files\\Claude Monitor\\ClaudeMonitor.exe"'

    def test_source_launch_uses_pythonw_and_the_packaging_entrypoint(self, tmp_path):
        interpreter = tmp_path / "python.exe"
        interpreter.write_text("")
        pythonw = tmp_path / "pythonw.exe"
        pythonw.write_text("")
        launcher = tmp_path / "project with spaces" / "run.py"

        command = autostart.startup_command(
            executable=interpreter,
            frozen=False,
            launcher=launcher,
        )

        assert command == subprocess.list2cmdline([str(pythonw), str(launcher)])


class TestStartupRegistration:
    def test_feature_path_registers_command_and_reports_enabled(
        self, fake_registry, monkeypatch
    ):
        monkeypatch.setattr(autostart, "startup_command", lambda: "expected command")

        autostart.set_enabled(True)

        assert fake_registry.value == "expected command"
        assert autostart.is_enabled() is True

    def test_disabling_removes_the_startup_value(self, fake_registry):
        fake_registry.value = "registered command"

        autostart.set_enabled(False)

        assert fake_registry.value is None
        assert fake_registry.deletes == 1

    def test_disabling_an_absent_value_is_idempotent(self, fake_registry):
        autostart.set_enabled(False)

        assert fake_registry.value is None

    def test_an_outdated_command_is_repaired_when_registered(
        self, fake_registry, monkeypatch
    ):
        fake_registry.value = "old location"
        monkeypatch.setattr(autostart, "startup_command", lambda: "new location")

        assert autostart.repair_if_enabled() is True

        assert fake_registry.value == "new location"
        assert fake_registry.writes == ["new location"]

    def test_disabled_startup_is_not_enabled_by_repair(
        self, fake_registry, monkeypatch
    ):
        monkeypatch.setattr(autostart, "startup_command", lambda: "expected command")

        assert autostart.repair_if_enabled() is False

        assert fake_registry.writes == []


def test_tray_toggle_registers_startup_and_updates_its_checkmark(
    fake_registry, monkeypatch
):
    """Exercise the complete user path from tray click to registry-backed UI state."""
    monkeypatch.setattr(autostart, "startup_command", lambda: "expected command")
    tray.init(
        threading.Event(),
        Path("."),
        startup_enabled=autostart.is_enabled,
        toggle_startup=lambda: main._toggle_startup_registration(
            autostart.is_enabled,
            autostart.set_enabled,
        ),
    )

    class Icon:
        def __init__(self):
            self.menu_updates = 0

        def update_menu(self):
            self.menu_updates += 1

    icon = Icon()
    state = DisplayState(
        icon_color="green",
        tooltip="usage",
        menu_status_label="Updated 1s ago",
        taskbar_text="80% (3h 0m)",
    )
    tray.apply(icon, state)
    menu_item = next(
        item for item in icon.menu.items if item.text == "Start with Windows"
    )
    assert menu_item.checked is False

    tray._on_toggle_startup(icon, menu_item)

    assert fake_registry.value == "expected command"
    assert menu_item.checked is True
    assert icon.menu_updates == 1
