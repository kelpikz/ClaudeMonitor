from __future__ import annotations

import ctypes
from ctypes import wintypes

from claudemonitor.taskbar_companion import Rect
from claudemonitor.win32_taskbar_window import (
    Win32TaskbarWindow,
    _WS_CHILD,
    _WS_POPUP,
)


# Windows' SWP_SHOWWINDOW bit. Repositioning must not pass it because that would
# override the user's separate visibility choice.
_WINDOW_FLAG_SHOW = 0x0040


class _FakeUser32:
    """Record the small subset of native calls used by the focused tests."""

    def __init__(self) -> None:
        self.style = _WS_POPUP
        self.parent_calls: list[tuple[int, int]] = []
        self.position_flags: list[int] = []

    def GetWindowLongPtrW(self, handle, index):
        return self.style

    def SetWindowLongPtrW(self, handle, index, style):
        previous_style = self.style
        self.style = style
        return previous_style

    def SetParent(self, handle, taskbar):
        self.parent_calls.append((handle, taskbar))
        return 99

    def SetWindowPos(self, handle, insert_after, left, top, width, height, flags):
        self.position_flags.append(flags)
        return True


def test_taskbar_attachment_converts_popup_style_to_child_style():
    native = Win32TaskbarWindow()
    fake_user32 = _FakeUser32()
    native._user32 = fake_user32

    assert native.attach_to_taskbar(30, 10) is True

    assert fake_user32.parent_calls == [(30, 10)]
    assert fake_user32.style & _WS_CHILD
    assert fake_user32.style & _WS_POPUP == 0


def test_repositioning_does_not_force_a_hidden_window_visible():
    native = Win32TaskbarWindow()
    fake_user32 = _FakeUser32()
    native._user32 = fake_user32

    native.move_window(30, Rect(1, 2, 101, 42), topmost=False)

    assert fake_user32.position_flags[-1] & _WINDOW_FLAG_SHOW == 0


def test_default_window_proc_accepts_pointer_sized_message_parameters():
    native = Win32TaskbarWindow()

    argument_types = native._user32.DefWindowProcW.argtypes

    assert argument_types == (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    assert ctypes.sizeof(argument_types[3]) == ctypes.sizeof(ctypes.c_void_p)
