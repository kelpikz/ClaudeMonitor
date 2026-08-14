from __future__ import annotations

import os
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import pystray
from PIL import Image

from .icon_art import tile_icon
from .models import DisplayState

_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (46, 160, 67),
    "amber": (210, 153, 34),
    "red": (218, 54, 51),
    "grey": (130, 130, 130),
}

_CONSOLE_URL = "https://console.anthropic.com/settings/usage"

# Windows' NOTIFYICONDATAW.szTip is a 128-WCHAR buffer; pystray raises
# ValueError above that, which would kill the poll thread. Cap below it (leaving
# room for the ellipsis marker) so an over-long tooltip degrades instead of
# crashing.
_MAX_TOOLTIP_LEN = 127

_TASKBAR_MENU_LABEL = "Show taskbar usage"
_TASKBAR_UNAVAILABLE_MENU_LABEL = "Show taskbar usage (unavailable — see log)"
_SESSION_REFRESH_MENU_LABEL = "Auto-refresh Claude session"
_STARTUP_MENU_LABEL = "Start with Windows"


class Toggle(Protocol):
    """A checkable menu entry: something that reports a boolean and flips it.

    ``settings.Setting`` satisfies this, and so does any two-line test double —
    which is the point of stating it as a protocol rather than importing the
    concrete type.
    """

    @property
    def enabled(self) -> bool: ...
    def toggle(self) -> bool: ...


@dataclass(frozen=True)
class TrayActions:
    """Everything the tray menu can reach, gathered into one value.

    Handed to the presenter at construction, so there is no init-before-use
    ordering for a caller to learn and no process-wide state for one tray to
    leak into the next.
    """

    manual_refresh: threading.Event
    log_dir: Path
    shutdown_requested: threading.Event | None = None
    taskbar: Toggle | None = None
    session_refresh: Toggle | None = None
    startup: Toggle | None = None
    taskbar_healthy: Callable[[], bool] | None = None


def _truncate_tooltip(text: str, limit: int = _MAX_TOOLTIP_LEN) -> str:
    """Clip a tooltip to the Windows tray limit, appending an ellipsis when cut,
    so pystray never raises 'string too long' and kills the poll thread."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class TrayPresenter:
    """Drive one pystray icon, tooltip, and menu from processed display state."""

    def __init__(self, actions: TrayActions) -> None:
        self._actions = actions
        # Rendered once so each poll only swaps images, and held per instance so
        # two presenters cannot see each other's tiles.
        self._icons: dict[str, Image.Image] = {
            name: tile_icon(fill) for name, fill in _COLORS.items()
        }

    def loading_icon(self) -> Image.Image:
        """Return the grey tile shown until the first fetch completes."""
        return self._icons["grey"]

    def apply(self, icon: pystray.Icon, state: DisplayState) -> None:
        """Show one processed state on the icon, tooltip, and menu."""
        icon.icon = self._icons[state.icon_color]
        icon.title = _truncate_tooltip(state.tooltip)
        icon.menu = self._build_menu(state.menu_status_label)

    def notify(self, icon: pystray.Icon, title: str, message: str) -> None:
        """Show a desktop notification through the active tray icon."""
        icon.notify(message, title=title)

    # --- Menu construction ------------------------------------------------

    def _build_menu(self, status_label: str) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", self._on_refresh),
            pystray.MenuItem("Open Anthropic console", self._on_open_console),
            pystray.MenuItem("Open log folder", self._on_open_log_folder),
            self._taskbar_menu_item(),
            self._toggle_menu_item(
                _SESSION_REFRESH_MENU_LABEL,
                self._actions.session_refresh,
                self._on_toggle_session_refresh,
            ),
            self._toggle_menu_item(
                _STARTUP_MENU_LABEL,
                self._actions.startup,
                self._on_toggle_startup,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _toggle_menu_item(
        self,
        label: str,
        toggle: Toggle | None,
        on_click: Callable[[pystray.Icon, pystray.MenuItem], None],
    ) -> pystray.MenuItem:
        """Build one checked menu entry backed by a Toggle."""
        return pystray.MenuItem(
            label,
            on_click,
            checked=lambda item: bool(toggle is not None and toggle.enabled),
        )

    def _taskbar_is_available(self) -> bool:
        """Return whether a native taskbar label is actually being shown."""
        healthy = self._actions.taskbar_healthy
        return healthy is None or healthy()

    def _taskbar_menu_item(self) -> pystray.MenuItem:
        """Build the taskbar toggle, disabled when no label can be displayed.

        Leaving a checked, clickable entry in place while the native window is
        gone would tell the user the feature is working when it is not.
        """
        available = self._taskbar_is_available()
        toggle = self._actions.taskbar
        return pystray.MenuItem(
            _TASKBAR_MENU_LABEL if available else _TASKBAR_UNAVAILABLE_MENU_LABEL,
            self._on_toggle_taskbar,
            checked=lambda item: bool(
                available and toggle is not None and toggle.enabled
            ),
            enabled=available,
        )

    # --- Menu callbacks ---------------------------------------------------

    def _on_refresh(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._actions.manual_refresh.set()

    def _on_open_console(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        webbrowser.open(_CONSOLE_URL)

    def _on_open_log_folder(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        os.startfile(str(self._actions.log_dir))

    def _on_toggle_taskbar(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Flip the taskbar label and refresh the menu checkmark."""
        self._flip(self._actions.taskbar, icon)

    def _on_toggle_session_refresh(
        self, icon: pystray.Icon, item: pystray.MenuItem
    ) -> None:
        """Flip the Claude CLI session nudge and refresh the menu checkmark."""
        self._flip(self._actions.session_refresh, icon)

    def _on_toggle_startup(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Flip Windows startup registration and refresh the menu checkmark."""
        self._flip(self._actions.startup, icon)

    def _flip(self, toggle: Toggle | None, icon: pystray.Icon) -> None:
        """Apply one toggle, then redraw the menu so its checkmark agrees.

        The menu is redrawn even when nothing is wired up, because pystray keeps
        showing the old checkmark until it is asked to update.
        """
        if toggle is not None:
            toggle.toggle()
        icon.update_menu()

    def _on_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """End the poll loop before asking pystray to join its setup thread."""
        if self._actions.shutdown_requested is not None:
            self._actions.shutdown_requested.set()
        self._actions.manual_refresh.set()
        icon.stop()
