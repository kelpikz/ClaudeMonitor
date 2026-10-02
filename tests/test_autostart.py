from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claudemonitor import autostart, main
from claudemonitor.config import Config
from claudemonitor.win32_bindings import (
    BM_GETCHECK,
    BM_SETCHECK,
    BST_CHECKED,
    IDOK,
    WM_COMMAND,
)
from claudemonitor.win32_settings_window import (
    _APPLY_ID,
    _FIRST_FIELD_ID,
    Win32SettingsWindow,
)


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


def _startup_window(monkeypatch):
    """Open a real settings window over fake DLLs, and find its startup box."""
    monkeypatch.setattr(autostart, "startup_command", lambda: "expected command")
    model = main.build_settings_model(
        companion=_UnusedCompanion(),
        pollers=[],
        config=Config(),
        log_dir=Path("."),
    )
    window = Win32SettingsWindow(model, uses_light_theme=lambda: True)
    window._user32 = _RecordingUser32()
    window._gdi32 = _RecordingDll()
    window._kernel32 = _RecordingDll()
    window._uxtheme = _RecordingDll()
    window._dwmapi = _RecordingDll()
    window._comctl32 = _RecordingDll()
    window._create()
    index = [field.key for field in model.fields()].index("startup")
    return window, index


def _tick_startup(window, index: int) -> None:
    """Tick the startup box the way Windows does, then report the click."""
    window._user32.checked[window._controls["startup"].label] = BST_CHECKED
    window._window_proc(window._handle, WM_COMMAND, _FIRST_FIELD_ID + index, 0)


def test_ticking_start_with_windows_writes_nothing_until_it_is_applied(
    fake_registry, monkeypatch
):
    """A tick is a proposal. Cancel has to be able to mean cancel."""
    window, index = _startup_window(monkeypatch)

    _tick_startup(window, index)

    assert fake_registry.value is None


def test_settings_window_registers_startup_and_updates_its_checkbox(
    fake_registry, monkeypatch
):
    """The complete user path: a click in the settings window to the registry.

    The box the window shows afterwards has to come back from the registry
    rather than from the click — otherwise a refused write would leave it lying.
    """
    window, index = _startup_window(monkeypatch)
    _tick_startup(window, index)
    window._user32.calls.clear()

    window._window_proc(window._handle, WM_COMMAND, _APPLY_ID, 0)

    assert fake_registry.value == "expected command"
    checks = [
        call
        for call in window._user32.calls
        if call[0] == "SendMessageW" and call[2] == BM_SETCHECK
    ]
    assert BST_CHECKED in [call[3] for call in checks]


def test_ok_registers_startup_and_closes_the_window(fake_registry, monkeypatch):
    window, index = _startup_window(monkeypatch)
    _tick_startup(window, index)

    window._window_proc(window._handle, WM_COMMAND, IDOK, 0)

    assert fake_registry.value == "expected command"
    assert any(call[0] == "DestroyWindow" for call in window._user32.calls)


class _RecordingDll:
    """Record every native call, returning a benign success value."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def __getattr__(self, name: str):
        def call(*args):
            self.calls.append((name, *args))
            return 1

        return call


class _RecordingUser32(_RecordingDll):
    """Hand out a fresh handle per created window, and remember what it holds.

    The window reads a checkbox and a number box back out of Windows, so a
    fake that forgets what it was told would answer every question with zero.
    """

    def __init__(self) -> None:
        super().__init__()
        self._next_handle = 500
        self.text: dict[int, str] = {}
        self.checked: dict[int, int] = {}

    def CreateWindowExW(self, style, class_name, text, *rest):
        self.calls.append(("CreateWindowExW", style, class_name, text, *rest))
        self._next_handle += 1
        self.text[self._next_handle] = text or ""
        return self._next_handle

    def SendMessageW(self, handle, message, wparam, lparam):
        self.calls.append(("SendMessageW", handle, message, wparam, lparam))
        if message == BM_SETCHECK:
            self.checked[handle] = wparam
        return self.checked.get(handle, 0) if message == BM_GETCHECK else 1

    def SetWindowTextW(self, handle, text):
        self.calls.append(("SetWindowTextW", handle, text))
        self.text[handle] = text
        return 1

    def GetWindowTextLengthW(self, handle):
        return len(self.text.get(handle, ""))

    def GetWindowTextW(self, handle, buffer, size):
        buffer.value = self.text.get(handle, "")
        return len(buffer.value)


class _UnusedCompanion:
    """The taskbar companion, which this path never touches."""

    visible = True
    healthy = True
