"""The two GDI chores every window here needs: the system UI font, and widths.

Both the taskbar label and the settings window have to draw in the typeface
Windows uses for ordinary UI text, at the scaling of the display they happen to
be on, and both have to measure a string before they can size themselves around
it. Neither is interesting, and getting either subtly wrong is what makes a
window look like it belongs to a different decade — so there is one of each.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

from .win32_bindings import (
    DEFAULT_GUI_FONT,
    FF_MODERN,
    FIXED_PITCH,
    LOGFONTW,
    NONCLIENTMETRICSW,
    SIZE,
    SPI_GETNONCLIENTMETRICS,
)

log = logging.getLogger(__name__)

# The fixed-width face every Windows since Vista ships with.
MONOSPACE_FACE = "Consolas"
# The size used when the system UI font cannot be read, in points.
_FALLBACK_POINT_SIZE = 9


def read_message_font_metrics(user32: Any, dpi: int) -> NONCLIENTMETRICSW | None:
    """Read the system UI font metrics as they apply at ``dpi``."""
    metrics = NONCLIENTMETRICSW()
    metrics.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)

    # SystemParametersInfoForDpi (Windows 10 1607) is the only variant that
    # answers for a display other than the one Windows considers primary.
    try:
        queried = user32.SystemParametersInfoForDpi(
            SPI_GETNONCLIENTMETRICS,
            ctypes.sizeof(NONCLIENTMETRICSW),
            ctypes.byref(metrics),
            0,
            dpi,
        )
    except (AttributeError, OSError):
        queried = 0

    # It refuses by returning zero rather than raising, and the stock font is a
    # visibly different typeface, so an unexplained refusal is worth one more
    # attempt at the system-wide metrics before giving that up.
    if not queried:
        queried = user32.SystemParametersInfoW(
            SPI_GETNONCLIENTMETRICS,
            ctypes.sizeof(NONCLIENTMETRICSW),
            ctypes.byref(metrics),
            0,
        )
    return metrics if queried else None


def create_message_font(user32: Any, gdi32: Any, dpi: int) -> tuple[int, bool]:
    """Build the system UI font for ``dpi``, and say whether it is Windows' own.

    A stock object is owned by Windows and must never be deleted, so the caller
    is told which kind it received.
    """
    metrics = read_message_font_metrics(user32, dpi)
    if metrics is None:
        log.warning("unable to read system UI font metrics; using the stock font")
        return gdi32.GetStockObject(DEFAULT_GUI_FONT), True
    return gdi32.CreateFontIndirectW(ctypes.byref(metrics.lfMessageFont)), False


def create_monospace_font(user32: Any, gdi32: Any, dpi: int) -> tuple[int, bool]:
    """Build a fixed-width font as tall as the system UI font at ``dpi``.

    It is always a font of its own, so the caller always releases it. The
    family asks Windows for another fixed-width face should Consolas be missing.
    """
    metrics = read_message_font_metrics(user32, dpi)
    if metrics is not None:
        description = LOGFONTW.from_buffer_copy(metrics.lfMessageFont)
    else:
        description = LOGFONTW()
        description.lfHeight = -round(_FALLBACK_POINT_SIZE * dpi / 72)
    description.lfFaceName = MONOSPACE_FACE
    description.lfPitchAndFamily = FIXED_PITCH | FF_MODERN
    return gdi32.CreateFontIndirectW(ctypes.byref(description)), False


def measure_text_width(gdi32: Any, font: int | None, text: str) -> int:
    """Return the pixel width ``font`` renders this text at.

    A memory device context needs no window of its own, so this can be called
    before a window exists to size it around its very first text.
    """
    device_context = gdi32.CreateCompatibleDC(None)
    try:
        gdi32.SelectObject(device_context, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(device_context, text, len(text), ctypes.byref(size))
        return size.cx
    finally:
        gdi32.DeleteDC(device_context)
