"""Drive the one tray icon that speaks for every provider at once.

There used to be an icon per provider. Two icons meant two menus, and every
app-wide setting had to be given to one of them and withheld from the other.
One icon, coloured by whichever provider is worst off, replaces both — and the
settings that once crowded its menu now live in the settings window.

A ``TrayPresenter`` is given everything its menu can reach when it is built,
so there is no call that must come first and no module state for one tray to
leak into the next.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import pystray
from PIL import Image

from .icon_art import tile_icon
from .models import TrayState

_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (46, 160, 67),
    "amber": (210, 153, 34),
    "red": (218, 54, 51),
    "grey": (130, 130, 130),
}

# Windows' NOTIFYICONDATAW.szTip is a 128-WCHAR buffer; pystray raises
# ValueError above that, which would kill the poll thread. Cap below it (leaving
# room for the ellipsis marker) so an over-long tooltip degrades instead of
# crashing.
_MAX_TOOLTIP_LEN = 127

_TASKBAR_MENU_LABEL = "Show taskbar usage"
_TASKBAR_UNAVAILABLE_MENU_LABEL = "Show taskbar usage (unavailable — see log)"
_SETTINGS_MENU_LABEL = "Settings…"


class TaskbarSwitch(Protocol):
    """The taskbar label's on/off switch, as the menu entry needs to see it.

    ``settings.SettingToggle`` satisfies this, so the menu and the settings
    window flip the label through one object.
    """

    @property
    def is_on(self) -> Callable[[], bool]: ...
    @property
    def write(self) -> Callable[[bool], None]: ...
    @property
    def available(self) -> Callable[[], bool]: ...


@dataclass(frozen=True)
class TrayActions:
    """Everything the tray menu can reach, gathered into one value."""

    manual_refresh: threading.Event
    shutdown_requested: threading.Event | None = None
    taskbar: TaskbarSwitch | None = None
    open_settings: Callable[[], None] | None = None


def _truncate_tooltip(text: str, limit: int = _MAX_TOOLTIP_LEN) -> str:
    """Clip a tooltip to the Windows tray limit, appending an ellipsis when cut,
    so pystray never raises 'string too long' and kills the poll thread."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class TrayPresenter:
    """Show the combined state of every provider on one pystray icon."""

    def __init__(self, actions: TrayActions) -> None:
        self._actions = actions
        # Drawn once so a poll only swaps images, and held per presenter so
        # two presenters cannot see each other's tiles.
        self._icons: dict[str, Image.Image] = {
            name: tile_icon(fill) for name, fill in _COLORS.items()
        }

    def loading_icon(self) -> Image.Image:
        """Return the grey placeholder tile shown before the first fetch lands."""
        return self._icons["grey"]

    def apply(self, icon: pystray.Icon, state: TrayState) -> None:
        """Show the combined state of every tracked provider on the tray icon."""
        icon.icon = self._tile(state.icon_color)
        icon.title = _truncate_tooltip(state.tooltip)
        icon.menu = self._build_menu(state.status_lines)

    def notify(self, icon: pystray.Icon, title: str, message: str) -> None:
        """Show a desktop notification through the active tray icon."""
        icon.notify(message, title=title)

    def _tile(self, color: str) -> Image.Image:
        """Return one rendered tile, falling back to grey for an unknown colour.

        A colour nobody drew must not raise inside the poll loop; grey at least
        says the reading cannot be trusted.
        """
        return self._icons.get(color) or self._icons["grey"]

    def _build_menu(self, status_lines: list[str]) -> pystray.Menu:
        """Build the menu: what every provider is doing, then the few live actions.

        Only what a user reaches for mid-task stays here. Everything else — the
        other switches, the log folder, the usage pages — is in the settings
        window, because a menu that scrolls is a menu nobody reads.
        """
        return pystray.Menu(
            *(pystray.MenuItem(line, None, enabled=False) for line in status_lines),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", self._on_refresh),
            self._taskbar_menu_item(),
            pystray.MenuItem(_SETTINGS_MENU_LABEL, self._on_open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _taskbar_is_available(self) -> bool:
        """Return whether a native taskbar label is actually being shown."""
        taskbar = self._actions.taskbar
        return taskbar is None or taskbar.available()

    def _taskbar_menu_item(self) -> pystray.MenuItem:
        """Build the taskbar toggle, disabled when no label can be displayed.

        Leaving a checked, clickable entry in place while the native window is
        gone would tell the user the feature is working when it is not.
        """
        available = self._taskbar_is_available()
        taskbar = self._actions.taskbar
        return pystray.MenuItem(
            _TASKBAR_MENU_LABEL if available else _TASKBAR_UNAVAILABLE_MENU_LABEL,
            self._on_toggle_taskbar,
            checked=lambda item: bool(
                available and taskbar is not None and taskbar.is_on()
            ),
            enabled=available,
        )

    def _on_refresh(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._actions.manual_refresh.set()

    def _on_open_settings(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Open the settings window, or raise the one already on screen."""
        if self._actions.open_settings is not None:
            self._actions.open_settings()

    def _on_toggle_taskbar(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Flip the taskbar label, then redraw the menu so its checkmark agrees.

        The menu is redrawn even with no switch wired up, because pystray keeps
        showing the old checkmark until it is asked to update.
        """
        taskbar = self._actions.taskbar
        if taskbar is not None:
            taskbar.write(not taskbar.is_on())
        icon.update_menu()

    def _on_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """End the poll loop before asking pystray to join its setup thread."""
        if self._actions.shutdown_requested is not None:
            self._actions.shutdown_requested.set()
        self._actions.manual_refresh.set()
        icon.stop()
