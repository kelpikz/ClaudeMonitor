from __future__ import annotations

import threading

from claudemonitor import tray
from claudemonitor.models import TrayState
from claudemonitor.tray import _MAX_TOOLTIP_LEN, _truncate_tooltip


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


def _menu_labels(menu) -> list[str]:
    return [item.text for item in menu.items]


def _applied_menu(**init_kwargs):
    """Initialise the tray, apply one state, and return the icon it produced."""
    tray.init(threading.Event(), **init_kwargs)
    icon = _FakeIcon()
    tray.apply(icon, _state())
    return icon


class TestTruncateTooltip:
    """The Windows tray tooltip (NOTIFYICONDATAW.szTip) is a fixed 128-WCHAR
    buffer; pystray raises ValueError above that, which would kill the poll
    thread. _truncate_tooltip guarantees we never exceed the limit."""

    def test_short_text_is_unchanged(self):
        text = "Claude  80% (3h 0m)"
        assert _truncate_tooltip(text) == text

    def test_text_at_the_limit_is_unchanged(self):
        text = "x" * _MAX_TOOLTIP_LEN
        assert _truncate_tooltip(text) == text

    def test_over_limit_is_clipped_within_bounds(self):
        result = _truncate_tooltip("y" * 200)
        assert len(result) <= _MAX_TOOLTIP_LEN

    def test_over_limit_keeps_an_ellipsis_marker(self):
        result = _truncate_tooltip("y" * 200)
        assert result.endswith("…")

    def test_limit_stays_within_windows_128_cap(self):
        # The hard Windows cap is 128; our limit must sit at or below it.
        assert _MAX_TOOLTIP_LEN <= 128


class TestApplyNeverExceedsTooltipLimit:
    """End-to-end guard: even a pathologically long tooltip must not raise."""

    def test_apply_truncates_long_tooltip(self):
        tray.init(threading.Event())
        icon = _FakeIcon()

        tray.apply(icon, _state(tooltip="z" * 500))

        assert len(icon.title) <= _MAX_TOOLTIP_LEN


class TestNotify:
    """Desktop notifications are passed through to the pystray icon."""

    def test_notify_uses_title_and_message(self):
        icon = _FakeIcon()
        tray.notify(icon, title="Claude usage below 50%", message="5h usage has 49% remaining.")

        assert icon.notifications == [
            ("Claude usage below 50%", "5h usage has 49% remaining.")
        ]


class TestQuit:
    """Quit must wake the background poll loop before stopping pystray."""

    def test_quit_requests_shutdown_and_wakes_the_waiting_poll_loop(self):
        manual_refresh = threading.Event()
        shutdown_requested = threading.Event()
        tray.init(manual_refresh, shutdown_requested)
        icon = _QuitIcon()

        tray._on_quit(icon, None)

        assert shutdown_requested.is_set()
        assert manual_refresh.is_set()
        assert icon.stop_calls == 1


class TestOneIconForEveryProvider:
    """One tile, coloured by the combined state, whoever it came from."""

    def test_the_colour_comes_from_the_combined_state(self):
        tray.init(threading.Event())
        green, red = _FakeIcon(), _FakeIcon()

        tray.apply(green, _state("green"))
        tray.apply(red, _state("red"))

        assert green.icon.tobytes() != red.icon.tobytes()

    def test_the_tooltip_is_the_combined_one(self):
        tray.init(threading.Event())
        icon = _FakeIcon()

        tray.apply(icon, _state(tooltip="Claude  80%\nCodex  43%"))

        assert icon.title == "Claude  80%\nCodex  43%"

    def test_an_unknown_colour_still_gets_a_tile(self):
        # A colour nobody drew must never raise inside the poll loop.
        tray.init(threading.Event())
        icon = _FakeIcon()

        tray.apply(icon, _state("chartreuse"))

        assert icon.icon is not None

    def test_loading_icon_requires_init_first(self):
        tray._icons.clear()

        try:
            tray.loading_icon()
        except RuntimeError:
            return
        raise AssertionError("loading_icon must refuse to guess before init()")

    def test_the_loading_icon_is_the_grey_tile(self):
        tray.init(threading.Event())

        assert tray.loading_icon().tobytes() == tray._tile("grey").tobytes()


class TestMenuContents:
    """The menu keeps what a user reaches for mid-task, and nothing else."""

    def test_every_provider_gets_its_own_status_line_first(self):
        tray.init(threading.Event())
        icon = _FakeIcon()

        tray.apply(
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
        tray.init(threading.Event())
        icon = _FakeIcon()

        tray.apply(icon, _state(status_lines=["Claude — Updated 5s ago"]))

        assert icon.menu.items[0].enabled is False

    def test_the_live_actions_are_offered(self):
        labels = _menu_labels(_applied_menu(taskbar_visible=lambda: True).menu)

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

    def _menu_item(self, **init_kwargs):
        icon = _applied_menu(**init_kwargs)
        return next(
            item
            for item in icon.menu.items
            if item.text
            in (tray._TASKBAR_MENU_LABEL, tray._TASKBAR_UNAVAILABLE_MENU_LABEL)
        )

    def test_checkmark_follows_the_companion_visibility(self):
        assert self._menu_item(taskbar_visible=lambda: True).checked is True
        assert self._menu_item(taskbar_visible=lambda: False).checked is False

    def test_an_unavailable_label_disables_the_entry(self):
        item = self._menu_item(
            taskbar_visible=lambda: True,
            taskbar_healthy=lambda: False,
        )

        assert item.enabled is False
        assert item.text == tray._TASKBAR_UNAVAILABLE_MENU_LABEL

    def test_toggle_runs_the_configured_action_and_refreshes_the_menu(self):
        toggles: list[None] = []
        tray.init(
            threading.Event(),
            taskbar_visible=lambda: True,
            toggle_taskbar=lambda: toggles.append(None),
        )
        icon = _FakeIcon()

        tray._on_toggle_taskbar(icon, None)

        assert toggles == [None]
        assert icon.menu_updates == 1


class TestSettingsMenuItem:
    """The long list of switches now lives behind one entry."""

    def test_clicking_it_opens_the_settings_window(self):
        opened: list[None] = []
        tray.init(threading.Event(), open_settings=lambda: opened.append(None))

        tray._on_open_settings(_FakeIcon(), None)

        assert opened == [None]

    def test_clicking_it_without_a_window_is_harmless(self):
        # A machine where the settings window could not be built still gets a
        # working tray, so the entry has to absorb the click.
        tray.init(threading.Event())

        tray._on_open_settings(_FakeIcon(), None)


class TestRefresh:
    """Refresh now wakes the poll loop rather than fetching on the UI thread."""

    def test_it_sets_the_manual_refresh_event(self):
        manual_refresh = threading.Event()
        tray.init(manual_refresh)

        tray._on_refresh(_FakeIcon(), None)

        assert manual_refresh.is_set()
