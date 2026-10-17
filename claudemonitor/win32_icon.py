"""Give a window icons made from PNG bytes.

The settings window is the only caller. Windows reads PNG data as an icon
resource, so each icon is drawn at the exact size Windows asks for and never
stretched. The icons belong to the caller, which frees them once the window
is gone.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Callable

from .win32_bindings import (
    ICON_BIG,
    ICON_RESOURCE_VERSION,
    ICON_SMALL,
    SM_CXICON,
    SM_CXSMICON,
    WM_SETICON,
)

log = logging.getLogger(__name__)

# The icon sizes at 96 DPI, for a Windows that will not say.
_FALLBACK_BIG_ICON_SIZE = 32
_FALLBACK_SMALL_ICON_SIZE = 16


def icon_from_png(user32: Any, png: bytes, size: int) -> int:
    """Ask Windows for an icon made from PNG bytes; 0 if it declines."""
    data = (ctypes.c_ubyte * len(png)).from_buffer_copy(png)
    return user32.CreateIconFromResourceEx(
        data, len(png), True, ICON_RESOURCE_VERSION, size, size, 0
    )


def set_window_icons(user32: Any, window: int, draw_png: Callable[[int], bytes]) -> list[int]:
    """Give a window its big and small icon, and return the icons made.

    The big one is for the taskbar and Alt+Tab, the small one for the title
    bar. An icon Windows cannot make is logged and left out.
    """
    icons: list[int] = []
    for which, metric, fallback in (
        (ICON_BIG, SM_CXICON, _FALLBACK_BIG_ICON_SIZE),
        (ICON_SMALL, SM_CXSMICON, _FALLBACK_SMALL_ICON_SIZE),
    ):
        size = user32.GetSystemMetrics(metric) or fallback
        icon = icon_from_png(user32, draw_png(size), size)
        if not icon:
            log.warning("Windows could not make the %spx window icon", size)
            continue
        icons.append(icon)
        user32.SendMessageW(window, WM_SETICON, which, icon)
    return icons


def destroy_icons(user32: Any, icons: list[int]) -> None:
    """Free icons made by set_window_icons, once their window is gone."""
    for icon in icons:
        user32.DestroyIcon(icon)
