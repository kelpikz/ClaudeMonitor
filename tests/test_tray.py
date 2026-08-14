from __future__ import annotations

import threading
from pathlib import Path

from claudemonitor.models import DisplayState
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


class _FakeToggle:
    """Stand in for a Setting: reports a live boolean and flips it."""

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self.toggles = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def toggle(self) -> bool:
        self._enabled = not self._enabled
        self.toggles += 1
        return self._enabled


def a_state(color: str = "green") -> DisplayState:
    """Build one processed state so tests only spell out what they care about."""
    return DisplayState(
        icon_color=color,
        tooltip="usage",
        menu_status_label="Updated 1s ago",
        taskbar_text="80% (3h 0m)",
    )


def a_presenter(**actions) -> TrayPresenter:
    """Build a presenter with the required actions defaulted."""
    actions.setdefault("manual_refresh", threading.Event())
    actions.setdefault("log_dir", Path("."))
    return TrayPresenter(TrayActions(**actions))


def menu_item(presenter: TrayPresenter, label: str):
    """Render the menu and pick out one entry by its visible text."""
    icon = _FakeIcon()
    presenter.apply(icon, a_state())
    return next(item for item in icon.menu.items if item.text == label)


# ===========================================================================
# The presenter replaces module-level state, so construction is the interface.
# ===========================================================================


class TestNoInitOrdering:
    """A presenter is usable the moment it is constructed.

    The previous module-level `init()` had to run before `apply()` or
    `loading_icon()`, and that ordering was part of the interface every caller
    and every test had to learn.
    """

    def test_the_loading_icon_is_available_immediately(self):
        assert a_presenter().loading_icon() is not None

    def test_apply_works_without_any_further_setup(self):
        icon = _FakeIcon()

        a_presenter().apply(icon, a_state())

        assert icon.title == "usage"

    def test_two_presenters_do_not_share_state(self):
        """Module globals leaked across tests; instance state cannot."""
        visible = _FakeToggle(enabled=True)
        hidden = _FakeToggle(enabled=False)

        shown = a_presenter(taskbar=visible)
        not_shown = a_presenter(taskbar=hidden)

        assert menu_item(shown, "Show taskbar usage").checked is True
        assert menu_item(not_shown, "Show taskbar usage").checked is False


class TestApplyDrivesTheIcon:
    """The status colour must reach the tray as a real image, not just a name."""

    def test_the_icon_image_changes_with_the_status_colour(self):
        presenter = a_presenter()
        icon = _FakeIcon()

        presenter.apply(icon, a_state("green"))
        green = icon.icon
        presenter.apply(icon, a_state("red"))

        assert icon.icon is not green

    def test_the_same_colour_reuses_one_rendered_tile(self):
        presenter = a_presenter()
        first, second = _FakeIcon(), _FakeIcon()

        presenter.apply(first, a_state("amber"))
        presenter.apply(second, a_state("amber"))

        assert first.icon is second.icon

    def test_every_status_colour_renders(self):
        presenter = a_presenter()
        icon = _FakeIcon()

        for color in ("green", "amber", "red", "grey"):
            presenter.apply(icon, a_state(color))
            assert icon.icon is not None

    def test_the_status_label_heads_the_menu(self):
        icon = _FakeIcon()

        a_presenter().apply(icon, a_state())

        assert icon.menu.items[0].text == "Updated 1s ago"


class TestTruncateTooltip:
    """The Windows tray tooltip (NOTIFYICONDATAW.szTip) is a fixed 128-WCHAR
    buffer; pystray raises ValueError above that, which would kill the poll
    thread. _truncate_tooltip guarantees we never exceed the limit."""

    def test_short_text_is_unchanged(self):
        text = "Claude usage\n5h: 70% left"
        assert _truncate_tooltip(text) == text

    def test_text_at_the_limit_is_unchanged(self):
        text = "x" * _MAX_TOOLTIP_LEN
        assert _truncate_tooltip(text) == text

    def test_over_limit_is_clipped_within_bounds(self):
        assert len(_truncate_tooltip("y" * 200)) <= _MAX_TOOLTIP_LEN

    def test_over_limit_keeps_an_ellipsis_marker(self):
        assert _truncate_tooltip("y" * 200).endswith("…")

    def test_limit_stays_within_windows_128_cap(self):
        assert _MAX_TOOLTIP_LEN <= 128

    def test_apply_truncates_a_pathologically_long_tooltip(self):
        icon = _FakeIcon()
        state = DisplayState(
            icon_color="grey",
            tooltip="z" * 500,
            menu_status_label="Updated 1s ago",
            taskbar_text="unavailable",
        )

        a_presenter().apply(icon, state)

        assert len(icon.title) <= _MAX_TOOLTIP_LEN


class TestNotify:
    def test_notify_uses_title_and_message(self):
        icon = _FakeIcon()

        a_presenter().notify(icon, title="Claude usage below 50%", message="49% left.")

        assert icon.notifications == [("Claude usage below 50%", "49% left.")]


class TestQuit:
    """Quit must wake the background poll loop before stopping pystray."""

    def test_quit_requests_shutdown_and_wakes_the_waiting_poll_loop(self):
        manual_refresh = threading.Event()
        shutdown_requested = threading.Event()
        presenter = a_presenter(
            manual_refresh=manual_refresh, shutdown_requested=shutdown_requested
        )
        icon = _QuitIcon()

        presenter._on_quit(icon, None)

        assert shutdown_requested.is_set()
        assert manual_refresh.is_set()
        assert icon.stop_calls == 1

    def test_quit_without_a_shutdown_event_still_stops_pystray(self):
        icon = _QuitIcon()

        a_presenter()._on_quit(icon, None)

        assert icon.stop_calls == 1


class TestRefresh:
    def test_refresh_now_wakes_the_poll_loop(self):
        manual_refresh = threading.Event()

        a_presenter(manual_refresh=manual_refresh)._on_refresh(_FakeIcon(), None)

        assert manual_refresh.is_set()


# ===========================================================================
# Menu toggles — six init parameters collapsed into three Toggles.
# ===========================================================================


class TestTaskbarMenuItem:
    """The 'Show taskbar usage' entry is the only way to toggle the label, so
    both the action and its checkmark must reflect the setting's real state."""

    def test_checkmark_follows_the_toggle(self):
        toggle = _FakeToggle(enabled=True)
        presenter = a_presenter(taskbar=toggle)

        assert menu_item(presenter, "Show taskbar usage").checked is True

        toggle.toggle()

        assert menu_item(presenter, "Show taskbar usage").checked is False

    def test_clicking_flips_the_toggle_and_refreshes_the_menu(self):
        toggle = _FakeToggle(enabled=True)
        presenter = a_presenter(taskbar=toggle)
        icon = _FakeIcon()

        presenter._on_toggle_taskbar(icon, None)

        assert toggle.toggles == 1
        assert toggle.enabled is False
        assert icon.menu_updates == 1

    def test_an_unwired_toggle_still_refreshes_the_menu(self):
        icon = _FakeIcon()

        a_presenter()._on_toggle_taskbar(icon, None)

        assert icon.menu_updates == 1

    def test_an_unhealthy_companion_disables_the_entry_and_says_why(self):
        presenter = a_presenter(
            taskbar=_FakeToggle(enabled=True), taskbar_healthy=lambda: False
        )

        item = menu_item(presenter, "Show taskbar usage (unavailable — see log)")

        assert item.enabled is False
        assert item.checked is False

    def test_a_healthy_companion_keeps_the_plain_label(self):
        presenter = a_presenter(
            taskbar=_FakeToggle(enabled=True), taskbar_healthy=lambda: True
        )

        assert menu_item(presenter, "Show taskbar usage").enabled is True


class TestSessionRefreshMenuItem:
    """The nudge spends real usage, so the tray must expose an obvious off switch."""

    def test_checkmark_follows_the_toggle(self):
        toggle = _FakeToggle(enabled=True)
        presenter = a_presenter(session_refresh=toggle)

        assert menu_item(presenter, "Auto-refresh Claude session").checked is True

        toggle.toggle()

        assert menu_item(presenter, "Auto-refresh Claude session").checked is False

    def test_clicking_flips_the_toggle_and_refreshes_the_menu(self):
        toggle = _FakeToggle(enabled=True)
        icon = _FakeIcon()

        a_presenter(session_refresh=toggle)._on_toggle_session_refresh(icon, None)

        assert toggle.toggles == 1
        assert icon.menu_updates == 1

    def test_an_unwired_toggle_still_refreshes_the_menu(self):
        icon = _FakeIcon()

        a_presenter()._on_toggle_session_refresh(icon, None)

        assert icon.menu_updates == 1


class TestStartWithWindowsMenuItem:
    """The tray exposes Windows startup registration as a checked toggle."""

    def test_checkmark_follows_the_toggle(self):
        toggle = _FakeToggle(enabled=False)
        presenter = a_presenter(startup=toggle)

        assert menu_item(presenter, "Start with Windows").checked is False

        toggle.toggle()

        assert menu_item(presenter, "Start with Windows").checked is True

    def test_clicking_flips_the_toggle_and_refreshes_the_menu(self):
        toggle = _FakeToggle(enabled=False)
        icon = _FakeIcon()

        a_presenter(startup=toggle)._on_toggle_startup(icon, None)

        assert toggle.toggles == 1
        assert toggle.enabled is True
        assert icon.menu_updates == 1

    def test_an_unwired_toggle_still_refreshes_the_menu(self):
        icon = _FakeIcon()

        a_presenter()._on_toggle_startup(icon, None)

        assert icon.menu_updates == 1
