"""Put text on the Windows clipboard.

The settings window's Copy command button is the only caller. The text goes in
as UTF-16, the format every program can paste, in memory that Windows takes
over once it accepts it.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

from .win32_bindings import (
    CF_UNICODETEXT,
    GMEM_MOVEABLE,
    KERNEL32_SIGNATURES,
    USER32_SIGNATURES,
    apply_signatures,
)

log = logging.getLogger(__name__)


def _load(name: str, signatures: dict) -> Any:
    """Load one DLL with its argument types declared."""
    dll = ctypes.WinDLL(name, use_last_error=True)
    apply_signatures(dll, signatures)
    return dll


def copy_text(text: str, *, user32: Any = None, kernel32: Any = None) -> bool:
    """Replace the clipboard's contents with one piece of text; False if Windows refused.

    Another program can hold the clipboard open for a moment, so a refusal is
    logged and reported rather than raised.
    """
    user32 = user32 or _load("user32", USER32_SIGNATURES)
    kernel32 = kernel32 or _load("kernel32", KERNEL32_SIGNATURES)
    if not user32.OpenClipboard(None):
        log.warning("the clipboard is in use by another program; nothing was copied")
        return False
    try:
        user32.EmptyClipboard()
        return _hand_over(text, user32, kernel32)
    finally:
        user32.CloseClipboard()


def _hand_over(text: str, user32: Any, kernel32: Any) -> bool:
    """Copy the text into movable memory and give that memory to the clipboard."""
    encoded = (text + "\0").encode("utf-16-le")
    memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not memory:
        log.warning("no memory for the clipboard text; nothing was copied")
        return False
    address = kernel32.GlobalLock(memory)
    if not address:
        kernel32.GlobalFree(memory)
        log.warning("the clipboard memory could not be locked; nothing was copied")
        return False
    ctypes.memmove(address, encoded, len(encoded))
    kernel32.GlobalUnlock(memory)
    if not user32.SetClipboardData(CF_UNICODETEXT, memory):
        # Windows owns the memory only once it has accepted it.
        kernel32.GlobalFree(memory)
        log.warning("Windows refused the clipboard text; nothing was copied")
        return False
    return True
