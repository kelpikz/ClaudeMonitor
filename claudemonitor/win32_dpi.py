"""Whose DPI this process claims, and what a 96-DPI constant is worth on it.

Both windows this application draws need these two answers, and so does the
settings layout, which owns no window at all. They lived inside the taskbar
adapter for as long as it was the only caller; they are their own module now
because three unrelated callers reaching into one window's adapter for a pure
scaling function says the function was never that window's.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

from .win32_bindings import (
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_UNAWARE,
    USER32_SIGNATURES,
    USER_DEFAULT_SCREEN_DPI,
    apply_signatures,
)

log = logging.getLogger(__name__)


def scale_for_dpi(value: int, dpi: int) -> int:
    """Convert a constant written for 96 DPI into pixels for a display at ``dpi``."""
    # GetDpiForWindow answers 0 for a handle Windows no longer recognizes, and
    # a zero scale factor would collapse the label to nothing.
    if dpi <= 0:
        return value
    return round(value * dpi / USER_DEFAULT_SCREEN_DPI)


def _user32_for_dpi() -> Any:
    """Return a user32 handle with the DPI calls' argument types declared.

    This runs before either window exists, because awareness has to be set
    before the process creates its first window — so it cannot borrow an
    adapter's already-prepared DLL. Declaring the signatures matters here more
    than anywhere else: without them ctypes passes the ``-4`` awareness context
    as a 32-bit int, and the truncated value silently fails to match any
    context Windows recognizes.
    """
    dll = ctypes.WinDLL("user32", use_last_error=True)
    apply_signatures(dll, USER32_SIGNATURES)
    return dll


def enable_per_monitor_dpi_awareness(user32: Any | None = None) -> bool:
    """Adopt the taskbar's DPI awareness, and report whether per-monitor was won.

    Explorer's taskbar is per-monitor aware. While ClaudeMonitor was unaware,
    Windows virtualized every coordinate crossing between the two, so a slot
    requested as 180x48 was applied as 144x38 on a 125% display and the label
    both mis-sized itself and kept its old scale after moving to a second
    monitor. Must be called before any window is created.
    """
    dll = _user32_for_dpi() if user32 is None else user32

    # SetProcessDpiAwarenessContext is Windows 10 1703 and later. It also fails
    # when awareness was already established, which is not worth dying over in
    # the first statement of the program.
    try:
        if dll.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return True
    except (AttributeError, OSError) as exc:
        log.info("per-monitor DPI awareness unavailable (%s); trying system DPI", exc)

    # Older builds offer only one process-wide, system-DPI setting. That still
    # stops coordinates being virtualized on a single-monitor machine.
    try:
        dll.SetProcessDPIAware()
    except (AttributeError, OSError) as exc:
        log.warning("unable to declare any DPI awareness (%s)", exc)
    return False


def process_dpi_awareness(user32: Any | None = None) -> int:
    """Return this process's DPI awareness using the same scale as a window's."""
    dll = _user32_for_dpi() if user32 is None else user32
    try:
        # A thread with no explicit context reports the process default, so the
        # current thread's context is the process's answer.
        context = dll.GetThreadDpiAwarenessContext()
        return dll.GetAwarenessFromDpiAwarenessContext(context)
    except (AttributeError, OSError):
        return DPI_AWARENESS_UNAWARE
