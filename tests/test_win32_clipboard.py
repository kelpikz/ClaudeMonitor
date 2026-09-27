"""Tests for putting text on the Windows clipboard.

The DLLs are replaced by recording fakes, so the order of the calls and the
bytes handed to Windows can be checked without touching the real clipboard.
"""

from __future__ import annotations

import ctypes

from claudemonitor.win32_bindings import CF_UNICODETEXT, GMEM_MOVEABLE
from claudemonitor.win32_clipboard import copy_text

_MEMORY = 7000  # The handle GlobalAlloc hands out.


class _FakeDll:
    """Record every call, answering each with a configurable result."""

    def __init__(self, **results) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.results = results

    def __getattr__(self, name: str):
        def call(*args):
            self.calls.append((name, *args))
            result = self.results.get(name, 1)
            return result(*args) if callable(result) else result

        return call

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def _dlls(**user32_results):
    """Build fake user32 and kernel32, with memory the copy can really write into."""
    memory = ctypes.create_string_buffer(256)
    kernel32 = _FakeDll(GlobalAlloc=_MEMORY, GlobalLock=ctypes.addressof(memory))
    user32 = _FakeDll(**user32_results)
    return user32, kernel32, memory


class TestCopyingText:
    def test_the_clipboard_is_opened_emptied_filled_and_closed_in_order(self):
        user32, kernel32, _memory = _dlls()

        assert copy_text("claude -p hi", user32=user32, kernel32=kernel32) is True
        assert user32.names() == [
            "OpenClipboard",
            "EmptyClipboard",
            "SetClipboardData",
            "CloseClipboard",
        ]

    def test_the_text_is_handed_over_as_unicode(self):
        user32, kernel32, _memory = _dlls()

        copy_text("claude -p hi", user32=user32, kernel32=kernel32)

        assert ("SetClipboardData", CF_UNICODETEXT, _MEMORY) in user32.calls

    def test_the_memory_holds_the_text_and_its_terminating_null(self):
        user32, kernel32, memory = _dlls()

        copy_text("hi", user32=user32, kernel32=kernel32)

        assert memory.raw[:6] == "hi\0".encode("utf-16-le")

    def test_the_memory_is_movable_and_sized_for_the_text(self):
        user32, kernel32, _memory = _dlls()

        copy_text("hi", user32=user32, kernel32=kernel32)

        assert ("GlobalAlloc", GMEM_MOVEABLE, 6) in kernel32.calls

    def test_the_memory_is_unlocked_before_windows_takes_it(self):
        user32, kernel32, _memory = _dlls()

        copy_text("hi", user32=user32, kernel32=kernel32)

        assert "GlobalUnlock" in kernel32.names()
        assert "GlobalFree" not in kernel32.names()  # Windows owns it now.

    def test_a_clipboard_another_program_holds_is_reported_not_raised(self):
        user32, kernel32, _memory = _dlls(OpenClipboard=0)

        assert copy_text("hi", user32=user32, kernel32=kernel32) is False
        assert user32.names() == ["OpenClipboard"]
        assert kernel32.calls == []

    def test_memory_that_cannot_be_had_closes_the_clipboard(self):
        user32, kernel32, _memory = _dlls()
        kernel32.results["GlobalAlloc"] = 0

        assert copy_text("hi", user32=user32, kernel32=kernel32) is False
        assert user32.names()[-1] == "CloseClipboard"

    def test_memory_that_cannot_be_locked_is_freed(self):
        user32, kernel32, _memory = _dlls()
        kernel32.results["GlobalLock"] = 0

        assert copy_text("hi", user32=user32, kernel32=kernel32) is False
        assert ("GlobalFree", _MEMORY) in kernel32.calls
        assert user32.names()[-1] == "CloseClipboard"

    def test_a_refused_hand_over_frees_the_memory(self):
        user32, kernel32, _memory = _dlls(SetClipboardData=0)

        assert copy_text("hi", user32=user32, kernel32=kernel32) is False
        assert ("GlobalFree", _MEMORY) in kernel32.calls
        assert user32.names()[-1] == "CloseClipboard"
