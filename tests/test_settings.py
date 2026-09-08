"""Tests for what the settings window contains and when it appears.

Everything here is the portable half: which tabs and groups the window offers,
what each field reads and writes, the rule that nothing is written until the
user says OK or Apply, and the rule that there is only ever one window. The
Windows drawing itself is covered by tests/test_win32_settings_window.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claudemonitor.settings import (
    Number,
    PendingSettings,
    SettingNumber,
    SettingToggle,
    SettingsWindowController,
    Switch,
    build_settings,
    parse_number,
)


def _switch(on: bool = True, available: bool = True):
    """Build a switch over a mutable flag, plus the flag itself to assert on."""
    state = {"on": on}
    switch = Switch(
        is_on=lambda: state["on"],
        write=lambda value: state.update(on=value),
        available=lambda: available,
    )
    return switch, state


def _number(value: int = 60, minimum: int = 1, maximum: int = 999):
    """Build a numeric setting over a mutable value, plus the value to assert on."""
    state = {"value": value}
    number = Number(
        value=lambda: state["value"],
        write=lambda new: state.update(value=new),
        minimum=minimum,
        maximum=maximum,
    )
    return number, state


def _settings(**overrides):
    """Build the production settings model with harmless defaults."""
    fields = {
        "taskbar": _switch()[0],
        "codex": _switch()[0],
        "session_refresh": _switch()[0],
        "startup": _switch()[0],
        "poll_interval": _number()[0],
        "amber_threshold": _number(50)[0],
        "red_threshold": _number(20)[0],
        "refresh_cooldown": _number(900)[0],
        "log_dir": Path("."),
        "open_url": lambda url: None,
        "open_folder": lambda path: None,
    }
    fields.update(overrides)
    return build_settings(**fields)


def _field(model, key: str):
    """Find one field anywhere in the model by its key."""
    return next(field for field in model.fields() if field.key == key)


class TestTheTabsAndGroups:
    """The window is a tabbed dialog, as TrafficMonitor's option dialog is."""

    def test_there_are_three_tabs(self):
        assert [tab.title for tab in _settings().tabs] == [
            "General",
            "Providers",
            "Taskbar",
        ]

    def test_every_tab_carries_at_least_one_group(self):
        assert all(tab.groups for tab in _settings().tabs)

    def test_every_group_carries_at_least_one_field(self):
        model = _settings()

        assert all(group.fields for tab in model.tabs for group in tab.groups)

    def test_the_general_tab_groups_startup_polling_thresholds_and_logs(self):
        general = _settings().tabs[0]

        assert [group.title for group in general.groups] == [
            "Startup",
            "Polling",
            "Icon colour",
            "Logs",
        ]

    def test_the_providers_tab_groups_sessions_claude_and_codex(self):
        providers = _settings().tabs[1]

        assert [group.title for group in providers.groups] == [
            "Sessions",
            "Claude",
            "Codex",
        ]

    def test_the_taskbar_tab_carries_the_label_switch(self):
        taskbar = _settings().tabs[2]

        assert [field.label for group in taskbar.groups for field in group.fields] == [
            "Show usage in the taskbar",
        ]


class TestWhichSettingsTheWindowOffers:
    """Every app-wide setting, including the ones that used to be file-only."""

    def _keys_of(self, kind) -> list[str]:
        return sorted(
            field.key for field in _settings().fields() if isinstance(field, kind)
        )

    def test_every_switch_is_offered(self):
        assert self._keys_of(SettingToggle) == sorted(
            ["startup", "session_refresh", "codex", "taskbar"]
        )

    def test_every_number_is_offered(self):
        assert self._keys_of(SettingNumber) == sorted(
            ["poll_interval", "amber_threshold", "red_threshold", "refresh_cooldown"]
        )

    def test_each_switch_reports_its_own_state(self):
        model = _settings(taskbar=_switch(on=False)[0], codex=_switch(on=True)[0])

        assert _field(model, "taskbar").is_on() is False
        assert _field(model, "codex").is_on() is True

    def test_each_number_reports_its_own_value(self):
        model = _settings(poll_interval=_number(30)[0])

        assert _field(model, "poll_interval").value() == 30

    def test_a_number_carries_the_range_it_accepts(self):
        model = _settings(poll_interval=_number(30, minimum=10, maximum=600)[0])
        interval = _field(model, "poll_interval")

        assert (interval.minimum, interval.maximum) == (10, 600)

    def test_a_number_carries_the_unit_it_is_measured_in(self):
        model = _settings()

        assert _field(model, "poll_interval").suffix == "seconds"
        assert _field(model, "amber_threshold").suffix == "%"

    def test_writing_a_switch_runs_that_setting_s_action(self):
        codex, state = _switch(on=True)

        _field(_settings(codex=codex), "codex").write(False)

        assert state["on"] is False

    def test_writing_a_number_runs_that_setting_s_action(self):
        interval, state = _number(60)

        _field(_settings(poll_interval=interval), "poll_interval").write(120)

        assert state["value"] == 120

    def test_an_unavailable_setting_is_shown_but_not_clickable(self):
        # The taskbar label can fail to appear at all; a checkbox that claims
        # otherwise would be a lie the user cannot act on.
        model = _settings(taskbar=_switch(available=False)[0])

        assert _field(model, "taskbar").available() is False

    def test_settings_are_available_by_default(self):
        model = _settings()

        assert all(
            field.available()
            for field in model.fields()
            if isinstance(field, SettingToggle)
        )


class TestTheLinks:
    """The three places a user goes to check on the app itself."""

    def test_every_link_is_offered(self):
        labels = [link.label for link in _settings().links()]

        assert labels == ["Open log folder", "Claude usage online", "Codex usage online"]

    def test_the_log_link_opens_the_log_directory(self):
        opened: list[str] = []
        model = _settings(log_dir=Path("C:/logs"), open_folder=opened.append)

        model.links()[0].open()

        assert opened == [str(Path("C:/logs"))]

    def test_the_usage_links_open_each_provider_s_page(self):
        opened: list[str] = []
        model = _settings(open_url=opened.append)

        model.links()[1].open()
        model.links()[2].open()

        assert opened == [
            "https://console.anthropic.com/settings/usage",
            "https://chatgpt.com/codex/settings/usage",
        ]


class TestParsingATypedNumber:
    """What the user types is held to the range the setting accepts."""

    def _field(self, minimum: int = 10, maximum: int = 600) -> SettingNumber:
        return SettingNumber(
            key="poll_interval",
            label="Check usage every",
            suffix="seconds",
            value=lambda: 60,
            write=lambda value: None,
            minimum=minimum,
            maximum=maximum,
        )

    def test_a_number_in_range_is_taken_as_typed(self):
        assert parse_number(self._field(), "120") == 120

    def test_a_number_below_the_range_is_raised_to_the_minimum(self):
        assert parse_number(self._field(), "1") == 10

    def test_a_number_above_the_range_is_lowered_to_the_maximum(self):
        assert parse_number(self._field(), "9999") == 600

    def test_surrounding_space_is_ignored(self):
        assert parse_number(self._field(), "  120  ") == 120

    def test_an_empty_box_is_not_a_number(self):
        # The user is mid-edit; the last usable value stands until they finish.
        assert parse_number(self._field(), "") is None

    def test_text_that_is_not_a_number_is_rejected(self):
        assert parse_number(self._field(), "soon") is None


class TestPendingSettings:
    """Nothing reaches the config file until the user says OK or Apply.

    This is the whole difference from the old window, where every click wrote
    immediately and Cancel was not a word the dialog knew.
    """

    def _model_and_state(self):
        taskbar, taskbar_state = _switch(on=True)
        interval, interval_state = _number(60)
        model = _settings(taskbar=taskbar, poll_interval=interval)
        return model, taskbar_state, interval_state

    def test_an_unedited_field_reads_the_stored_value(self):
        model, _taskbar, _interval = self._model_and_state()

        pending = PendingSettings(model)

        assert pending.value_of("taskbar") is True
        assert pending.value_of("poll_interval") == 60

    def test_an_edited_field_reads_back_what_was_typed(self):
        model, _taskbar, _interval = self._model_and_state()
        pending = PendingSettings(model)

        pending.edit("poll_interval", 120)

        assert pending.value_of("poll_interval") == 120

    def test_editing_writes_nothing_until_applied(self):
        model, taskbar_state, interval_state = self._model_and_state()
        pending = PendingSettings(model)

        pending.edit("taskbar", False)
        pending.edit("poll_interval", 120)

        assert taskbar_state["on"] is True
        assert interval_state["value"] == 60

    def test_applying_writes_every_edited_field(self):
        model, taskbar_state, interval_state = self._model_and_state()
        pending = PendingSettings(model)
        pending.edit("taskbar", False)
        pending.edit("poll_interval", 120)

        pending.apply()

        assert taskbar_state["on"] is False
        assert interval_state["value"] == 120

    def test_applying_leaves_untouched_fields_alone(self):
        written: list[str] = []
        codex = Switch(is_on=lambda: True, write=lambda value: written.append("codex"))

        PendingSettings(_settings(codex=codex)).apply()

        assert written == []

    def test_a_value_edited_back_to_what_it_already_was_is_not_written(self):
        written: list[bool] = []
        taskbar = Switch(is_on=lambda: True, write=written.append)
        pending = PendingSettings(_settings(taskbar=taskbar))

        pending.edit("taskbar", False)
        pending.edit("taskbar", True)
        pending.apply()

        assert written == []

    def test_applying_twice_writes_once(self):
        written: list[int] = []
        interval = Number(value=lambda: 60, write=written.append, minimum=1, maximum=999)
        pending = PendingSettings(_settings(poll_interval=interval))
        pending.edit("poll_interval", 120)

        pending.apply()
        pending.apply()

        assert written == [120]

    def test_discarding_throws_the_edits_away(self):
        model, taskbar_state, _interval = self._model_and_state()
        pending = PendingSettings(model)
        pending.edit("taskbar", False)

        pending.discard()
        pending.apply()

        assert taskbar_state["on"] is True

    def test_discarding_returns_the_field_to_its_stored_value(self):
        model, _taskbar, _interval = self._model_and_state()
        pending = PendingSettings(model)
        pending.edit("poll_interval", 120)

        pending.discard()

        assert pending.value_of("poll_interval") == 60

    def test_an_unedited_window_is_not_dirty(self):
        assert PendingSettings(_settings()).is_dirty() is False

    def test_an_edited_window_is_dirty(self):
        pending = PendingSettings(_settings())

        pending.edit("poll_interval", 120)

        assert pending.is_dirty() is True

    def test_a_field_edited_back_to_its_stored_value_is_not_dirty(self):
        pending = PendingSettings(_settings(poll_interval=_number(60)[0]))

        pending.edit("poll_interval", 120)
        pending.edit("poll_interval", 60)

        assert pending.is_dirty() is False

    def test_an_unknown_key_reads_as_nothing_rather_than_raising(self):
        # The window routes by control id; a stale id must not take the dialog
        # down with it.
        assert PendingSettings(_settings()).value_of("no-such-setting") is None

    def test_editing_an_unknown_key_is_ignored(self):
        pending = PendingSettings(_settings())

        pending.edit("no-such-setting", True)

        assert pending.is_dirty() is False


class TestApplyingSurvivesAFailure:
    """A setting that cannot be saved must not take the others down with it."""

    def test_one_broken_write_is_logged_and_the_rest_still_apply(self, caplog):
        def refuse(value):
            raise OSError("the registry is locked")

        startup = Switch(is_on=lambda: False, write=refuse)
        interval, interval_state = _number(60)
        pending = PendingSettings(_settings(startup=startup, poll_interval=interval))
        pending.edit("startup", True)
        pending.edit("poll_interval", 120)

        with caplog.at_level(logging.ERROR):
            pending.apply()

        assert "startup" in caplog.text
        assert interval_state["value"] == 120

    def test_a_field_that_cannot_be_read_is_treated_as_unset(self, caplog):
        def unreadable():
            raise OSError("the registry is locked")

        startup = Switch(is_on=unreadable, write=lambda value: None)
        pending = PendingSettings(_settings(startup=startup))

        with caplog.at_level(logging.ERROR):
            value = pending.value_of("startup")

        assert value is False
        assert "startup" in caplog.text


class _FakeWindow:
    """Stands in for the native window: records shows, focuses, and closes."""

    def __init__(self, focusable: bool = True, fails: Exception | None = None):
        self.shows = 0
        self.focuses = 0
        self.closes = 0
        self._focusable = focusable
        self._fails = fails

    def show(self) -> None:
        self.shows += 1
        if self._fails is not None:
            raise self._fails

    def focus(self) -> bool:
        self.focuses += 1
        return self._focusable

    def close(self) -> None:
        self.closes += 1


class TestSettingsWindowController:
    """One window at a time, opened off the tray's own message loop.

    Building the window on pystray's thread would freeze the menu until the
    window closed, so it gets a thread of its own; and a second click has to
    raise the window already on screen rather than stack another one behind it.
    """

    def _controller(self, *windows, run=None):
        """Build a controller that hands out the given fake windows in turn."""
        made = iter(windows)
        pending: list = []
        controller = SettingsWindowController(
            build_window=lambda: next(made),
            start_background=run if run is not None else pending.append,
        )
        return controller, pending

    def test_opening_shows_a_window(self):
        window = _FakeWindow()
        controller, _pending = self._controller(window, run=lambda work: work())

        controller.open()

        assert window.shows == 1

    def test_the_window_runs_off_the_calling_thread(self):
        window = _FakeWindow()
        controller, pending = self._controller(window)

        controller.open()

        assert window.shows == 0
        pending[0]()
        assert window.shows == 1

    def test_a_second_click_raises_the_window_already_open(self):
        first, second = _FakeWindow(), _FakeWindow()
        controller, pending = self._controller(first, second)

        controller.open()
        controller.open()

        assert first.focuses == 1
        assert second.shows == 0
        assert len(pending) == 1

    def test_a_closed_window_can_be_opened_again(self):
        first, second = _FakeWindow(), _FakeWindow()
        controller, _pending = self._controller(first, second, run=lambda work: work())

        controller.open()  # show() returns as soon as the window closes
        controller.open()

        assert second.shows == 1

    def test_a_window_that_will_not_come_forward_is_not_replaced(self, caplog):
        # Windows refuses SetForegroundWindow while another app holds the
        # foreground lock, and flashes the taskbar button instead. The window
        # is on screen either way, so a second one would only confuse.
        stuck, replacement = _FakeWindow(focusable=False), _FakeWindow()
        controller, pending = self._controller(stuck, replacement)

        controller.open()
        with caplog.at_level(logging.WARNING):
            controller.open()

        assert len(pending) == 1
        assert "settings window" in caplog.text

    def test_a_window_that_fails_to_open_is_logged_and_forgotten(self, caplog):
        broken, second = _FakeWindow(fails=OSError("no window station")), _FakeWindow()
        controller, _pending = self._controller(broken, second, run=lambda work: work())

        with caplog.at_level(logging.ERROR):
            controller.open()

        assert "settings window" in caplog.text
        controller.open()
        assert second.shows == 1

    def test_a_window_that_cannot_be_built_is_logged_not_raised(self, caplog):
        # open() runs inside the tray's message loop; an escaping error there
        # is an invisible traceback and a menu entry that does nothing.
        def unavailable():
            raise OSError("no window station")

        controller = SettingsWindowController(
            build_window=unavailable,
            start_background=lambda work: work(),
        )

        with caplog.at_level(logging.ERROR):
            controller.open()

        assert "settings window" in caplog.text

    def test_a_window_that_raises_while_being_raised_is_only_logged(self, caplog):
        class _Angry(_FakeWindow):
            def focus(self) -> bool:
                raise OSError("the handle is gone")

        angry, replacement = _Angry(), _FakeWindow()
        controller, pending = self._controller(angry, replacement)
        controller.open()

        with caplog.at_level(logging.ERROR):
            controller.open()

        assert len(pending) == 1
        assert "settings window" in caplog.text

    def test_a_click_while_the_first_window_is_still_opening_adds_nothing(self):
        # The window is created on its own thread, so a fast second click can
        # arrive before there is a handle to raise.
        first, second = _FakeWindow(focusable=False), _FakeWindow()
        controller, pending = self._controller(first, second)

        controller.open()
        controller.open()

        assert second.shows == 0

    def test_closing_the_app_closes_an_open_window(self):
        window = _FakeWindow()
        controller, pending = self._controller(window)
        controller.open()

        controller.close()

        assert window.closes == 1

    def test_closing_without_a_window_is_harmless(self):
        controller, _pending = self._controller()

        controller.close()
