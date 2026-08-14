from __future__ import annotations

import logging

from claudemonitor.settings import (
    Setting,
    session_refresh_setting,
    startup_setting,
    taskbar_setting,
)


class _FakeCompanion:
    """Stand in for the taskbar companion, which owns its own live visibility."""

    def __init__(self, visible: bool = True):
        self.visible = visible

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class _FakeNudger:
    """Stand in for the session nudger, which owns its own live enabled flag."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


class _RecordingSaver:
    """Capture persisted settings, or fail every write when given an error."""

    def __init__(self, raises: Exception | None = None):
        self.saved: list[tuple[str, str, object]] = []
        self._raises = raises

    def __call__(self, section: str, key: str, value: object) -> None:
        if self._raises is not None:
            raise self._raises
        self.saved.append((section, key, value))


class _FakeRegistry:
    """Stand in for the Windows startup registration, which reads through."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def read(self) -> bool:
        return self.enabled

    def write(self, enabled: bool) -> None:
        self.enabled = enabled


# ===========================================================================
# Setting — the one shape every user-toggleable setting shares.
# ===========================================================================


class TestSetting:
    """Setting: reads through to the live value and writes without ever raising.

    Every toggle runs inside pystray's message loop, where an escaping exception
    surfaces only as a stderr traceback nobody sees in a windowed build.
    """

    def test_enabled_reports_what_the_reader_returns(self):
        setting = Setting("thing", read=lambda: True, write=lambda value: None)

        assert setting.enabled is True

    def test_enabled_reflects_a_later_change_rather_than_a_snapshot(self):
        live = [False]
        setting = Setting("thing", read=lambda: live[0], write=lambda value: None)

        live[0] = True

        assert setting.enabled is True

    def test_toggle_writes_the_opposite_of_the_current_value(self):
        written: list[bool] = []
        setting = Setting("thing", read=lambda: True, write=written.append)

        setting.toggle()

        assert written == [False]

    def test_toggle_returns_the_new_value(self):
        live = [False]

        def write(value: bool) -> None:
            live[0] = value

        setting = Setting("thing", read=lambda: live[0], write=write)

        assert setting.toggle() is True

    def test_an_unreadable_setting_reports_disabled_instead_of_raising(self, caplog):
        def unavailable() -> bool:
            raise OSError("registry unavailable")

        setting = Setting("startup", read=unavailable, write=lambda value: None)

        with caplog.at_level(logging.ERROR):
            enabled = setting.enabled

        assert enabled is False
        assert "startup" in caplog.text

    def test_a_failing_write_is_logged_rather_than_raised(self, caplog):
        def unwritable(_value: bool) -> None:
            raise OSError("config file is locked")

        setting = Setting("taskbar", read=lambda: True, write=unwritable)

        with caplog.at_level(logging.ERROR):
            setting.toggle()

        assert "taskbar" in caplog.text

    def test_a_write_that_half_applied_keeps_the_part_that_landed(self):
        """The live effect is applied before persistence, so a failed save must
        not roll back a toggle the user has already seen take effect."""
        live = [True]

        def write(value: bool) -> None:
            live[0] = value
            raise OSError("config file is locked")

        setting = Setting("taskbar", read=lambda: live[0], write=write)

        assert setting.toggle() is False
        assert live[0] is False


# ===========================================================================
# The three settings the tray actually exposes.
# ===========================================================================


class TestTaskbarSetting:
    """The taskbar label's visibility, read from the companion that owns it."""

    def test_reads_the_companions_live_visibility(self):
        companion = _FakeCompanion(visible=False)

        assert taskbar_setting(companion, save=_RecordingSaver()).enabled is False

    def test_toggling_hides_the_companion_and_persists_the_choice(self):
        companion = _FakeCompanion(visible=True)
        saver = _RecordingSaver()

        taskbar_setting(companion, save=saver).toggle()

        assert companion.visible is False
        assert saver.saved == [("taskbar", "enabled", False)]

    def test_a_failing_save_still_hides_the_companion(self, caplog):
        companion = _FakeCompanion(visible=True)
        setting = taskbar_setting(companion, save=_RecordingSaver(OSError("locked")))

        with caplog.at_level(logging.ERROR):
            setting.toggle()

        assert companion.visible is False

    def test_toggling_twice_returns_to_the_original_visibility(self):
        companion = _FakeCompanion(visible=True)
        setting = taskbar_setting(companion, save=_RecordingSaver())

        setting.toggle()
        setting.toggle()

        assert companion.visible is True

    def test_the_setting_never_goes_stale_against_the_companion(self):
        """Nothing caches the value, so a companion changed elsewhere still reads true."""
        companion = _FakeCompanion(visible=True)
        setting = taskbar_setting(companion, save=_RecordingSaver())

        companion.set_visible(False)

        assert setting.enabled is False


class TestSessionRefreshSetting:
    """The Claude CLI nudge, read from the nudger that owns it."""

    def test_reads_the_nudgers_live_setting(self):
        nudger = _FakeNudger(enabled=False)

        assert session_refresh_setting(nudger, save=_RecordingSaver()).enabled is False

    def test_toggling_disables_the_nudger_and_persists_the_choice(self):
        nudger = _FakeNudger(enabled=True)
        saver = _RecordingSaver()

        session_refresh_setting(nudger, save=saver).toggle()

        assert nudger.enabled is False
        assert saver.saved == [("session_refresh", "enabled", False)]

    def test_a_failing_save_still_flips_the_nudger(self, caplog):
        nudger = _FakeNudger(enabled=True)
        setting = session_refresh_setting(nudger, save=_RecordingSaver(OSError("ro")))

        with caplog.at_level(logging.ERROR):
            setting.toggle()

        assert nudger.enabled is False


class TestStartupSetting:
    """Windows startup registration, which reads and writes the registry directly."""

    def test_reads_the_registry(self):
        registry = _FakeRegistry(enabled=True)

        assert startup_setting(registry.read, registry.write).enabled is True

    def test_toggling_registers_the_startup_command(self):
        registry = _FakeRegistry(enabled=False)

        startup_setting(registry.read, registry.write).toggle()

        assert registry.enabled is True

    def test_toggling_an_enabled_registration_removes_it(self):
        registry = _FakeRegistry(enabled=True)

        startup_setting(registry.read, registry.write).toggle()

        assert registry.enabled is False

    def test_an_unreadable_registry_reports_not_registered(self, caplog):
        def unavailable() -> bool:
            raise OSError("registry unavailable")

        setting = startup_setting(unavailable, lambda enabled: None)

        with caplog.at_level(logging.ERROR):
            enabled = setting.enabled

        assert enabled is False

    def test_an_unwritable_registry_does_not_crash_the_menu_callback(self, caplog):
        def unavailable(_enabled: bool) -> None:
            raise OSError("registry unavailable")

        setting = startup_setting(lambda: False, unavailable)

        with caplog.at_level(logging.ERROR):
            setting.toggle()

        assert "startup" in caplog.text
