from __future__ import annotations

import os
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import pystray
from PIL import Image, ImageDraw

from .models import DisplayState

if TYPE_CHECKING:
    pass

_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (46, 160, 67),
    "amber": (210, 153, 34),
    "red": (218, 54, 51),
    "grey": (130, 130, 130),
}

_CONSOLE_URL = "https://console.anthropic.com/settings/usage"

_icons: dict[str, Image.Image] = {}
_manual_refresh: threading.Event | None = None
_log_dir: Path | None = None


def init(manual_refresh: threading.Event, log_dir: Path) -> None:
    global _manual_refresh, _log_dir
    _manual_refresh = manual_refresh
    _log_dir = log_dir
    _build_icons()


def loading_icon() -> Image.Image:
    if not _icons:
        raise RuntimeError("tray.init() must be called before loading_icon()")
    return _icons["grey"]


def _build_icons() -> None:
    for name, fill in _COLORS.items():
        border = tuple(int(c * 0.7) for c in fill)
        img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([1, 1, 14, 14], fill=fill, outline=border)
        _icons[name] = img


def apply(icon: pystray.Icon, state: DisplayState) -> None:
    icon.icon = _icons[state.icon_color]
    icon.title = state.tooltip
    icon.menu = _build_menu(state.menu_status_label)


def _build_menu(status_label: str) -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem(status_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", _on_refresh),
        pystray.MenuItem("Open Anthropic console", _on_open_console),
        pystray.MenuItem("Open log folder", _on_open_log_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )


def _on_refresh(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    if _manual_refresh is not None:
        _manual_refresh.set()


def _on_open_console(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    webbrowser.open(_CONSOLE_URL)


def _on_open_log_folder(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    if _log_dir is not None:
        os.startfile(str(_log_dir))


def _on_quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    icon.stop()
