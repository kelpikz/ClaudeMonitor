"""Drive the one tray icon that speaks for every provider at once.

There used to be an icon per provider. Two icons meant two menus, and every
app-wide setting had to be given to one of them and withheld from the other.
One icon, coloured by whichever provider is worst off, replaces both — and the
settings that once crowded its menu now live in the settings window.

``init()`` must be called before ``apply()``: the status tiles are drawn once
at startup so a poll only ever swaps an already-rendered image.
"""

from __future__ import annotations

import threading
from typing import Callable

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

# One tile per status colour, keyed by colour name.
_icons: dict[str, Image.Image] = {}
_manual_refresh: threading.Event | None = None
_shutdown_requested: threading.Event | None = None
_taskbar_visible: Callable[[], bool] | None = None
_toggle_taskbar: Callable[[], None] | None = None
_taskbar_healthy: Callable[[], bool] | None = None
_open_settings: Callable[[], None] | None = None

_TASKBAR_MENU_LABEL = "Show taskbar usage"
_TASKBAR_UNAVAILABLE_MENU_LABEL = "Show taskbar usage (unavailable — see log)"
_SETTINGS_MENU_LABEL = "Settings…"


def init(
    manual_refresh: threading.Event,
    shutdown_requested: threading.Event | None = None,
    taskbar_visible: Callable[[], bool] | None = None,
    toggle_taskbar: Callable[[], None] | None = None,
    taskbar_healthy: Callable[[], bool] | None = None,
    open_settings: Callable[[], None] | None = None,
) -> None:
    """Prepare tray dependencies, including the event that ends the poll loop."""
    global _manual_refresh, _shutdown_requested
    global _taskbar_visible, _toggle_taskbar, _taskbar_healthy, _open_settings
    _manual_refresh = manual_refresh
    _shutdown_requested = shutdown_requested
    _taskbar_visible = taskbar_visible
    _toggle_taskbar = toggle_taskbar
    _taskbar_healthy = taskbar_healthy
    _open_settings = open_settings
    _build_icons()


def loading_icon() -> Image.Image:
    """Return the grey placeholder tile shown before the first fetch lands."""
    if not _icons:
        raise RuntimeError("tray.init() must be called before loading_icon()")
    return _icons["grey"]


def _build_icons() -> None:
    """Render every status tile once, so a poll only swaps images."""
    for name, fill in _COLORS.items():
        _icons[name] = tile_icon(fill)


def _tile(color: str) -> Image.Image:
    """Return one rendered tile, falling back to grey for an unknown colour.

    A colour nobody drew must not raise inside the poll loop; grey at least
    says the reading cannot be trusted.
    """
    return _icons.get(color) or _icons["grey"]


def _truncate_tooltip(text: str, limit: int = _MAX_TOOLTIP_LEN) -> str:
    """Clip a tooltip to the Windows tray limit, appending an ellipsis when cut,
    so pystray never raises 'string too long' and kills the poll thread."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def apply(icon: pystray.Icon, state: TrayState) -> None:
    """Show the combined state of every tracked provider on the tray icon."""
    icon.icon = _tile(state.icon_color)
    icon.title = _truncate_tooltip(state.tooltip)
    icon.menu = _build_menu(state.status_lines)


def notify(icon: pystray.Icon, title: str, message: str) -> None:
    """Show a desktop notification through the active tray icon."""
    icon.notify(message, title=title)


def _build_menu(status_lines: list[str]) -> pystray.Menu:
    """Build the menu: what every provider is doing, then the few live actions.

    Only what a user reaches for mid-task stays here. Everything else — the
    other switches, the log folder, the usage pages — is in the settings window,
    because a menu that scrolls is a menu nobody reads.
    """
    return pystray.Menu(
        *(pystray.MenuItem(line, None, enabled=False) for line in status_lines),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", _on_refresh),
        _taskbar_menu_item(),
        pystray.MenuItem(_SETTINGS_MENU_LABEL, _on_open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )


def _taskbar_is_available() -> bool:
    """Return whether a native taskbar label is actually being shown."""
    return _taskbar_healthy is None or _taskbar_healthy()


def _taskbar_menu_item() -> pystray.MenuItem:
    """Build the taskbar toggle, disabled when no label can be displayed.

    Leaving a checked, clickable entry in place while the native window is gone
    would tell the user the feature is working when it is not.
    """
    available = _taskbar_is_available()
    return pystray.MenuItem(
        _TASKBAR_MENU_LABEL if available else _TASKBAR_UNAVAILABLE_MENU_LABEL,
        _on_toggle_taskbar,
        checked=lambda item: bool(
            available and _taskbar_visible and _taskbar_visible()
        ),
        enabled=available,
    )


def _on_refresh(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    if _manual_refresh is not None:
        _manual_refresh.set()


def _on_open_settings(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Open the settings window, or raise the one already on screen."""
    if _open_settings is not None:
        _open_settings()


def _on_toggle_taskbar(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """Toggle the companion and refresh the menu checkmark."""
    if _toggle_taskbar is not None:
        _toggle_taskbar()
    icon.update_menu()


def _on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    """End the poll loop before asking pystray to join its setup thread."""
    if _shutdown_requested is not None:
        _shutdown_requested.set()
    if _manual_refresh is not None:
        _manual_refresh.set()
    icon.stop()
