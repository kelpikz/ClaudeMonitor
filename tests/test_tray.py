from __future__ import annotations

import threading

from claudemonitor import tray
from claudemonitor.models import TrayState
from claudemonitor.settings import SettingToggle
from claudemonitor.tray import (
    _MAX_TOOLTIP_LEN,
    TrayActions,
    TrayPresenter,
    _truncate_tooltip,
)


class _FakeIcon:
    """Records whatever apply() assigns, standing in for a real pystray.Icon."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.menu = None
        self.notifications = []
        self.menu_updates = 0

    def notify(self, message, title=None):
        self.notifications.append((title, message))

    def update_menu(self):
        self.menu_updates += 1


class _QuitIcon:
    """Records whether the tray Quit action asks pystray to stop."""

    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


def _state(
    icon_color: str = "green",
    tooltip: str = "Claude  80% (3h 0m)",
    status_lines: list[str] | None = None,
) -> TrayState:
    """Build the combined tray state, spelling out only what a test cares about."""
    return TrayState(
        icon_color=icon_color,
        tooltip=tooltip,
        status_lines=status_lines
        if status_lines is not None
        else ["Claude — Updated 1s ago"],
    )


def _presenter(**action_kwargs) -> TrayPresenter:
    """Build a presenter whose actions are only what a test spells out."""
    action_kwargs.setdefault("manual_refresh", threading.Event())
    return TrayPresenter(TrayActions(**action_kwargs))


def _taskbar(visible: bool = True, healthy: bool = True) -> SettingToggle:
    """A taskbar switch that remembers what it was last set to."""
    state = {"visible": visible}
    return SettingToggle(
        is_on=lambda: state["visible"],
        write=lambda value: state.update(visible=value),
        available=lambda: healthy,
    )


def _menu_labels(menu) -> list[str]:
    return [item.text for item in menu.items]


def _applied_menu(**action_kwargs):
    """Build a presenter, apply one state, and return the icon it produced."""
    icon = _FakeIcon()
    _presenter(**action_kwargs).apply(icon, _state())
    return icon


class TestTruncateTooltip:
    """The Windows tray tooltip (NOTIFYICONDATAW.szTip) is a fixed 128-WCHAR
    buffer; pystray raises ValueError above that, which would kill the poll
    thread. _truncate_tooltip guarantees we never exceed the limit."""

    def test_short_text_is_unchanged(self):
        text = "Claude  80% (3h 0m)"
        assert _truncate_tooltip(text) == text

    def test_over_limit_is_clipped_within_bounds(self):
        result = _truncate_tooltip("y" * 200)
        assert len(result) <= _MAX_TOOLTIP_LEN

    def test_over_limit_keeps_an_ellipsis_marker(self):
        result = _truncate_tooltip("y" * 200)
        assert result.endswith("…")


class TestApplyNeverExceedsTooltipLimit:
    """End-to-end guard: even a pathologically long tooltip must not raise."""

    def test_apply_truncates_long_tooltip(self):
        icon = _FakeIcon()

        _presenter().apply(icon, _state(tooltip="z" * 500))

        assert len(icon.title) <= _MAX_TOOLTIP_LEN


class TestNotify:
    """Desktop notifications are passed through to the pystray icon."""

    def test_notify_uses_title_and_message(self):
        icon = _FakeIcon()

        _presenter().notify(
            icon, title="Claude usage below 50%", message="5h usage has 49% remaining."
        )

        assert icon.notifications == [
            ("Claude usage below 50%", "5h usage has 49% remaining.")
        ]


class TestQuit:
    """Quit must wake the background poll loop before stopping pystray."""

    def test_quit_requests_shutdown_and_wakes_the_waiting_poll_loop(self):
        manual_refresh = threading.Event()
        shutdown_requested = threading.Event()
        presenter = _presenter(
            manual_refresh=manual_refresh, shutdown_requested=shutdown_requested
        )
        icon = _QuitIcon()

        presenter._on_quit(icon, None)

        assert shutdown_requested.is_set()
        assert manual_refresh.is_set()
        assert icon.stop_calls == 1


class TestOneIconForEveryProvider:
    """One tile, coloured by the combined state, whoever it came from."""

    def test_the_colour_comes_from_the_combined_state(self):
        presenter = _presenter()
        green, red = _FakeIcon(), _FakeIcon()

        presenter.apply(green, _state("green"))
        presenter.apply(red, _state("red"))

        assert green.icon.tobytes() != red.icon.tobytes()

    def test_each_colour_gets_the_tile_drawn_for_it(self):
        # The status colour is the headline feature: a red reading must put the
        # red tile on the tray, not only the word "red" in a DisplayState.
        presenter = _presenter()
        icon = _FakeIcon()

        for colour, fill in tray._COLORS.items():
            presenter.apply(icon, _state(colour))
            assert icon.icon.tobytes() == tray.tile_icon(fill).tobytes()

    def test_the_tooltip_is_the_combined_one(self):
        icon = _FakeIcon()

        _presenter().apply(icon, _state(tooltip="Claude  80%\nCodex  43%"))

        assert icon.title == "Claude  80%\nCodex  43%"

    def test_an_unknown_colour_still_gets_a_tile(self):
        # A colour nobody drew must never raise inside the poll loop.
        icon = _FakeIcon()

        _presenter().apply(icon, _state("chartreuse"))

        assert icon.icon is not None

    def test_the_loading_icon_is_the_grey_tile(self):
        presenter = _presenter()
        icon = _FakeIcon()

        presenter.apply(icon, _state("grey"))

        assert presenter.loading_icon().tobytes() == icon.icon.tobytes()


class TestNoInitOrdering:
    """A presenter is ready the moment it exists, and owns its own state."""

    def test_the_loading_icon_needs_no_earlier_call(self):
        assert TrayPresenter(TrayActions(threading.Event())).loading_icon() is not None

    def test_two_presenters_keep_their_own_actions(self):
        first_refresh, second_refresh = threading.Event(), threading.Event()
        first = _presenter(manual_refresh=first_refresh)
        _presenter(manual_refresh=second_refresh)

        first._on_refresh(_FakeIcon(), None)

        assert first_refresh.is_set()
        assert not second_refresh.is_set()


class TestMenuContents:
    """The menu keeps what a user reaches for mid-task, and nothing else."""

    def test_every_provider_gets_its_own_status_line_first(self):
        icon = _FakeIcon()

        _presenter().apply(
            icon,
            _state(
                status_lines=["Claude — Updated 5s ago", "Codex — Updated 5s ago"]
            ),
        )

        assert _menu_labels(icon.menu)[:2] == [
            "Claude — Updated 5s ago",
            "Codex — Updated 5s ago",
        ]

    def test_status_lines_are_not_clickable(self):
        icon = _FakeIcon()

        _presenter().apply(icon, _state(status_lines=["Claude — Updated 5s ago"]))

        assert icon.menu.items[0].enabled is False

    def test_the_live_actions_are_offered(self):
        labels = _menu_labels(_applied_menu(taskbar=_taskbar()).menu)

        assert "Refresh now" in labels
        assert tray._TASKBAR_MENU_LABEL in labels
        assert tray._SETTINGS_MENU_LABEL in labels
        assert "Quit" in labels

    def test_the_settings_that_moved_are_gone_from_the_menu(self):
        # These now live in the settings window; leaving them here as well
        # would give the user two switches for one setting.
        labels = _menu_labels(_applied_menu().menu)

        for moved in (
            "Start with Windows",
            "Show Codex usage",
            "Auto-refresh idle sessions",
            "Open log folder",
            "Open Anthropic console",
        ):
            assert moved not in labels


class TestTaskbarMenuItem:
    """The 'Show taskbar usage' entry is the only way to toggle the label, so
    both the action and its checkmark must reflect the companion's real state."""

    def _menu_item(self, **action_kwargs):
        icon = _applied_menu(**action_kwargs)
        return next(
            item
            for item in icon.menu.items
            if item.text
            in (tray._TASKBAR_MENU_LABEL, tray._TASKBAR_UNAVAILABLE_MENU_LABEL)
        )

    def test_checkmark_follows_the_companion_visibility(self):
        assert self._menu_item(taskbar=_taskbar(visible=True)).checked is True
        assert self._menu_item(taskbar=_taskbar(visible=False)).checked is False

    def test_an_unavailable_label_disables_the_entry(self):
        item = self._menu_item(taskbar=_taskbar(visible=True, healthy=False))

        assert item.enabled is False
        assert item.text == tray._TASKBAR_UNAVAILABLE_MENU_LABEL

    def test_toggle_flips_the_switch_and_refreshes_the_menu(self):
        taskbar = _taskbar(visible=True)
        presenter = _presenter(taskbar=taskbar)
        icon = _FakeIcon()

        presenter._on_toggle_taskbar(icon, None)

        assert taskbar.is_on() is False
        assert icon.menu_updates == 1

    def test_toggle_without_a_switch_still_refreshes_the_menu(self):
        icon = _FakeIcon()

        _presenter()._on_toggle_taskbar(icon, None)

        assert icon.menu_updates == 1


class TestSettingsMenuItem:
    """The long list of switches now lives behind one entry."""

    def test_clicking_it_opens_the_settings_window(self):
        opened: list[None] = []
        presenter = _presenter(open_settings=lambda: opened.append(None))

        presenter._on_open_settings(_FakeIcon(), None)

        assert opened == [None]

    def test_clicking_it_without_a_window_is_harmless(self):
        # A machine where the settings window could not be built still gets a
        # working tray, so the entry has to absorb the click.
        _presenter()._on_open_settings(_FakeIcon(), None)


class TestRefresh:
    """Refresh now wakes the poll loop rather than fetching on the UI thread."""

    def test_it_sets_the_manual_refresh_event(self):
        manual_refresh = threading.Event()

        _presenter(manual_refresh=manual_refresh)._on_refresh(_FakeIcon(), None)

        assert manual_refresh.is_set()
