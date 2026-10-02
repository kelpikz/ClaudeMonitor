"""Tests for what the settings window contains and when it appears.

Everything here is the portable half: which tabs and groups the window offers,
what each field reads and writes, the rule that nothing is written until the
user says OK or Apply, and the rule that there is only ever one window. The
Windows drawing itself is covered by tests/test_win32_settings_window.py.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from claudemonitor.models import CLAUDE, CODEX
from claudemonitor.settings import (
    ProviderFields,
    PendingSettings,
    SettingChoice,
    SettingCommand,
    SettingLink,
    SettingNumber,
    SettingOutput,
    SettingText,
    SettingToggle,
    SettingsModel,
    SettingsTab,
    SettingsWindowController,
    build_settings,
    parse_number,
)


def _switch(on: bool = True, available: bool = True):
    """Build a switch over a mutable flag, plus the flag itself to assert on."""
    state = {"on": on}
    switch = SettingToggle(
        is_on=lambda: state["on"],
        write=lambda value: state.update(on=value),
        available=lambda: available,
    )
    return switch, state


def _number(value: int = 60, minimum: int = 1, maximum: int = 999):
    """Build a numeric setting over a mutable value, plus the value to assert on."""
    state = {"value": value}
    number = SettingNumber(
        value=lambda: state["value"],
        write=lambda new: state.update(value=new),
        minimum=minimum,
        maximum=maximum,
    )
    return number, state


def _text(value: str = ""):
    """Build a text setting over a mutable value, plus the value to assert on."""
    state = {"value": value}
    text = SettingText(
        value=lambda: state["value"], write=lambda new: state.update(value=new)
    )
    return text, state


def _choice(value: str = ""):
    """Build a choice over a mutable value, plus the value to assert on."""
    state = {"value": value}
    choice = SettingChoice(
        value=lambda: state["value"], write=lambda new: state.update(value=new)
    )
    return choice, state


def _provider_fields(provider, tracking=None, **overrides):
    """Build one provider's tab with harmless defaults."""
    fields = {
        "provider": provider,
        "tracking": tracking if tracking is not None else _switch()[0],
        "renew_token": _switch()[0],
        "wake_session": _switch()[0],
        "cooldown": _number(900, 60, 86_400)[0],
        "model": _text("haiku")[0],
        "effort": _choice("low")[0],
        "copy_command": _command()[0],
        "run_command": _command()[0],
        "last_run": SettingOutput(text=lambda: "Not run yet."),
    }
    fields.update(overrides)
    return ProviderFields(**fields)


def _command():
    """Build a command button, plus the list of (model, effort) it was given."""
    given: list[tuple[str, str]] = []
    command = SettingCommand(
        act=lambda model, effort, changed: given.append((model, effort))
    )
    return command, given


def _providers(codex=None):
    """Build both providers' tabs."""
    return [_provider_fields(CLAUDE), _provider_fields(CODEX, tracking=codex)]


def _settings(**overrides):
    """Build the production settings model with harmless defaults."""
    codex = overrides.pop("codex", None)
    fields = {
        "taskbar": _switch()[0],
        "providers": _providers(codex),
        "startup": _switch()[0],
        "poll_interval": _number()[0],
        "amber_threshold": _number(50)[0],
        "red_threshold": _number(20)[0],
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

    def test_the_tabs_are_general_providers_and_taskbar(self):
        assert [tab.title for tab in _settings().tabs] == [
            "General",
            "Providers",
            "Taskbar",
        ]

    def test_every_tab_carries_at_least_one_group_or_section(self):
        assert all(tab.groups or tab.sections for tab in _settings().tabs)

    def test_every_group_carries_at_least_one_field(self):
        model = _settings()

        assert all(group.fields for group in _every_group(model))

    def test_the_general_tab_groups_startup_polling_thresholds_and_logs(self):
        general = _settings().tabs[0]

        assert [group.title for group in general.groups] == [
            "Startup",
            "Polling",
            "Icon colour",
            "Logs",
        ]

    def test_the_taskbar_tab_is_last(self):
        assert _settings().tabs[-1].title == "Taskbar"

    def test_the_taskbar_tab_carries_the_label_switch(self):
        taskbar = _settings().tabs[2]

        assert [field.label for group in taskbar.groups for field in group.fields] == [
            "Show usage in the taskbar",
        ]


def _every_group(model):
    """Every group in the model, whether a tab holds it or one of its sections does."""
    return [
        group
        for tab in model.tabs
        for group in [*tab.groups, *(g for section in tab.sections for g in section.groups)]
    ]


def _providers_tab(model=None):
    """The one tab that lists the providers down its left side."""
    return next(tab for tab in (model or _settings()).tabs if tab.title == "Providers")


class TestTheProvidersTab:
    """Every provider is one entry in a list, rather than a tab of its own.

    A tab each would widen the strip with every provider added, and Grok or
    Antigravity would each cost a tab.
    """

    def test_the_providers_tab_has_no_groups_of_its_own(self):
        assert _providers_tab().groups == []

    def test_there_is_one_section_per_provider_in_order(self):
        assert [section.title for section in _providers_tab().sections] == [
            "Claude",
            "Codex",
        ]

    def test_a_third_provider_is_a_third_section(self):
        third = replace(CODEX, key="grok", label="Grok")
        model = _settings(providers=[*_providers(), _provider_fields(third)])

        assert [section.title for section in _providers_tab(model).sections] == [
            "Claude",
            "Codex",
            "Grok",
        ]

    def test_a_provider_section_groups_tracking_and_refresh(self):
        claude = _providers_tab().sections[0]

        assert [group.title for group in claude.groups] == [
            "Tracking",
            "Auto-refresh",
            "Last run",
        ]

    def test_the_last_run_group_holds_only_its_box(self):
        # The window hides a group that holds only empty boxes, so nothing
        # else may share it.
        last_run = _providers_tab().sections[0].groups[2]

        assert [field.key for field in last_run.fields] == ["claude_last_run"]

    def test_a_provider_section_holds_every_one_of_its_settings_in_order(self):
        codex = _providers_tab().sections[1]

        assert [field.key for group in codex.groups for field in group.fields] == [
            "codex_tracking",
            "codex_usage",
            "codex_renew_token",
            "codex_wake_session",
            "codex_cooldown",
            "codex_model",
            "codex_effort",
            "codex_copy_command",
            "codex_run_command",
            "codex_last_run",
        ]

    def test_a_provider_section_names_its_provider(self):
        claude = _providers_tab().sections[0]

        assert [field.label for group in claude.groups for field in group.fields][:2] == [
            "Track Claude usage",
            "Claude usage online",
        ]

    def test_the_refresh_group_says_what_each_setting_does(self):
        refresh = _providers_tab().sections[0].groups[1]

        assert [field.label for field in refresh.fields] == [
            "Renew an expired token",
            "Start an idle 5-hour window",
            "Wait between runs",
            "Model",
            "Reasoning effort",
            "Copy command",
            "Run now",
        ]

    def test_adding_a_provider_is_not_offered_yet(self):
        # The button exists in the window; it is switched off in the model
        # until there is a provider that is not already listed.
        assert _providers_tab().add_section is None

    def test_a_section_field_is_among_the_model_s_fields(self):
        keys = [field.key for field in _settings().fields()]

        assert "claude_tracking" in keys and "codex_effort" in keys

    def test_fields_run_tab_by_tab_and_section_by_section(self):
        keys = [field.key for field in _settings().fields()]

        assert keys.index("red_threshold") < keys.index("claude_tracking")
        assert keys.index("claude_effort") < keys.index("codex_tracking")
        assert keys.index("codex_effort") < keys.index("taskbar")

    def test_the_add_button_is_not_a_field(self):
        # It adds a provider rather than holding a setting, so a pending edit
        # has nothing to say about it.
        add = SettingLink(key="add_provider", label="Add provider…", open=lambda: None)
        model = SettingsModel(tabs=[SettingsTab(title="Providers", add_section=add)])

        assert model.fields() == []


class TestWhichSettingsTheWindowOffers:
    """Every app-wide setting, including the ones that used to be file-only."""

    def _keys_of(self, kind) -> list[str]:
        return sorted(
            field.key for field in _settings().fields() if isinstance(field, kind)
        )

    def test_every_switch_is_offered(self):
        assert self._keys_of(SettingToggle) == sorted(
            [
                "startup",
                "taskbar",
                "claude_tracking",
                "claude_renew_token",
                "claude_wake_session",
                "codex_tracking",
                "codex_renew_token",
                "codex_wake_session",
            ]
        )

    def test_every_number_is_offered(self):
        assert self._keys_of(SettingNumber) == sorted(
            [
                "poll_interval",
                "amber_threshold",
                "red_threshold",
                "claude_cooldown",
                "codex_cooldown",
            ]
        )

    def test_every_model_box_is_offered(self):
        assert self._keys_of(SettingText) == ["claude_model", "codex_model"]

    def test_every_effort_list_is_offered(self):
        assert self._keys_of(SettingChoice) == ["claude_effort", "codex_effort"]

    def test_each_switch_reports_its_own_state(self):
        model = _settings(taskbar=_switch(on=False)[0], codex=_switch(on=True)[0])

        assert _field(model, "taskbar").is_on() is False
        assert _field(model, "codex_tracking").is_on() is True

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

        _field(_settings(codex=codex), "codex_tracking").write(False)

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


class TestTheRefreshFields:
    """Model, effort, and the last run: what the user decides the nudge with."""

    def test_the_model_box_reads_and_writes_its_setting(self):
        model_text, state = _text("haiku")
        model = _settings(
            providers=[_provider_fields(CLAUDE, model=model_text), _provider_fields(CODEX)]
        )
        box = _field(model, "claude_model")

        assert box.value() == "haiku"
        box.write("sonnet")

        assert state["value"] == "sonnet"

    def _with_default(self, provider, default_model):
        """Build the settings with one provider's CLI default replaced."""
        other = CODEX if provider is CLAUDE else CLAUDE
        return _settings(
            providers=[
                _provider_fields(replace(provider, default_model=default_model)),
                _provider_fields(other),
            ]
        )

    def test_an_empty_model_box_names_the_model_the_cli_will_use(self):
        model = self._with_default(CODEX, lambda: "gpt-6.1-sol")

        assert _field(model, "codex_model").placeholder() == "gpt-6.1-sol (default)"

    def test_a_default_that_cannot_be_named_says_the_cli_chooses(self):
        model = self._with_default(CLAUDE, lambda: "")

        assert _field(model, "claude_model").placeholder() == "Claude CLI default"

    def test_the_default_is_read_again_each_time(self):
        # The window is built once at startup, but the CLI's config can change.
        defaults = iter(["gpt-6-sol", "gpt-6.1-sol"])
        box = _field(self._with_default(CODEX, lambda: next(defaults)), "codex_model")

        assert box.placeholder() == "gpt-6-sol (default)"
        assert box.placeholder() == "gpt-6.1-sol (default)"

    def test_a_default_that_cannot_be_read_says_the_cli_chooses(self):
        def unreadable():
            raise OSError("config.toml is locked")

        box = _field(self._with_default(CODEX, unreadable), "codex_model")

        assert box.placeholder() == "Codex CLI default"

    def test_the_effort_list_offers_the_provider_s_own_levels(self):
        effort = _field(_settings(), "codex_effort")

        assert [value for value, _label in effort.choices] == list(CODEX.effort_levels)

    def test_the_cli_s_own_default_is_offered_by_name(self):
        effort = _field(_settings(), "claude_effort")

        assert effort.choices[0] == ("", "Default (low)")

    def test_each_named_level_is_shown_as_it_is_spelled(self):
        effort = _field(_settings(), "claude_effort")

        assert ("xhigh", "xhigh") in effort.choices

    def test_the_effort_list_reads_and_writes_its_setting(self):
        effort_choice, state = _choice("low")
        model = _settings(
            providers=[_provider_fields(CLAUDE), _provider_fields(CODEX, effort=effort_choice)]
        )
        effort = _field(model, "codex_effort")

        assert effort.value() == "low"
        effort.write("high")

        assert state["value"] == "high"

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
        codex = SettingToggle(is_on=lambda: True, write=lambda value: written.append("codex"))

        PendingSettings(_settings(codex=codex)).apply()

        assert written == []

    def test_a_value_edited_back_to_what_it_already_was_is_not_written(self):
        written: list[bool] = []
        taskbar = SettingToggle(is_on=lambda: True, write=written.append)
        pending = PendingSettings(_settings(taskbar=taskbar))

        pending.edit("taskbar", False)
        pending.edit("taskbar", True)
        pending.apply()

        assert written == []

    def test_applying_twice_writes_once(self):
        written: list[int] = []
        interval = SettingNumber(value=lambda: 60, write=written.append, minimum=1, maximum=999)
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


class TestPendingText:
    """A typed model and a picked effort wait for Apply, as a number does."""

    def _pending(self):
        model_text, text_state = _text("haiku")
        effort, effort_state = _choice("")
        model = _settings(
            providers=[
                _provider_fields(CLAUDE, model=model_text, effort=effort),
                _provider_fields(CODEX),
            ]
        )
        return PendingSettings(model), text_state, effort_state

    def test_a_typed_model_is_held_until_applied(self):
        pending, text_state, _effort = self._pending()

        pending.edit("claude_model", "sonnet")

        assert pending.value_of("claude_model") == "sonnet"
        assert text_state["value"] == "haiku"

    def test_applying_writes_the_typed_model_and_the_picked_effort(self):
        pending, text_state, effort_state = self._pending()
        pending.edit("claude_model", "sonnet")
        pending.edit("claude_effort", "low")

        pending.apply()

        assert text_state["value"] == "sonnet"
        assert effort_state["value"] == "low"

    def test_typing_the_stored_model_back_is_not_a_change(self):
        pending, _text_state, _effort = self._pending()
        pending.edit("claude_model", "sonnet")

        pending.edit("claude_model", "haiku")

        assert pending.is_dirty() is False

class TestApplyingSurvivesAFailure:
    """A setting that cannot be saved must not take the others down with it."""

    def test_one_broken_write_is_logged_and_the_rest_still_apply(self, caplog):
        def refuse(value):
            raise OSError("the registry is locked")

        startup = SettingToggle(is_on=lambda: False, write=refuse)
        interval, interval_state = _number(60)
        pending = PendingSettings(_settings(startup=startup, poll_interval=interval))
        pending.edit("startup", True)
        pending.edit("poll_interval", 120)

        with caplog.at_level(logging.ERROR):
            pending.apply()

        assert "startup" in caplog.text
        assert interval_state["value"] == 120

    def test_a_text_that_cannot_be_read_is_treated_as_empty(self, caplog):
        def unreadable():
            raise OSError("config vanished")

        model_text = SettingText(value=unreadable, write=lambda value: None)
        pending = PendingSettings(
            _settings(
                providers=[_provider_fields(CLAUDE, model=model_text), _provider_fields(CODEX)]
            )
        )

        with caplog.at_level(logging.ERROR):
            value = pending.value_of("claude_model")

        assert value == ""

    def test_a_field_that_cannot_be_read_is_treated_as_unset(self, caplog):
        def unreadable():
            raise OSError("the registry is locked")

        startup = SettingToggle(is_on=unreadable, write=lambda value: None)
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


class TestTheCommandFields:
    """Copy command, Run now, and the box that says what the last one did."""

    def test_both_buttons_act_on_the_model_and_effort_of_their_own_section(self):
        model = _settings()

        for key in ("claude_copy_command", "claude_run_command"):
            button = _field(model, key)
            assert (button.model_key, button.effort_key) == ("claude_model", "claude_effort")

    def test_a_second_provider_s_buttons_read_its_own_fields(self):
        button = _field(_settings(), "codex_run_command")

        assert (button.model_key, button.effort_key) == ("codex_model", "codex_effort")

    def test_the_copy_button_runs_the_action_it_was_given(self):
        copy, given = _command()
        model = _settings(
            providers=[_provider_fields(CLAUDE, copy_command=copy), _provider_fields(CODEX)]
        )

        _field(model, "claude_copy_command").act("haiku", "low", lambda: None)

        assert given == [("haiku", "low")]

    def test_the_run_button_runs_the_action_it_was_given(self):
        run, given = _command()
        model = _settings(
            providers=[_provider_fields(CLAUDE), _provider_fields(CODEX, run_command=run)]
        )

        _field(model, "codex_run_command").act("", "high", lambda: None)

        assert given == [("", "high")]

    def test_the_output_box_shows_the_text_it_was_given(self):
        output = SettingOutput(text=lambda: "Succeeded in 6.3 s.")
        model = _settings(
            providers=[_provider_fields(CLAUDE, last_run=output), _provider_fields(CODEX)]
        )

        assert _field(model, "claude_last_run").text() == "Succeeded in 6.3 s."

    def test_neither_the_buttons_nor_the_box_hold_a_setting(self):
        # Nothing about them is written on OK, so a pending edit ignores them.
        keys = {field.key for field in _settings().editable()}

        assert "claude_copy_command" not in keys
        assert "claude_run_command" not in keys
        assert "claude_last_run" not in keys

    def test_the_buttons_are_not_links(self):
        assert "Copy command" not in [link.label for link in _settings().links()]
