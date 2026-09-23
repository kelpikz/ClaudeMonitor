from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

import pytest
from PIL import Image

from claudemonitor.models import CLAUDE, CODEX, LabelSegment, Rect
from claudemonitor import win32_bindings
from claudemonitor.win32_bindings import (
    AC_SRC_ALPHA,
    ANTIALIASED_QUALITY,
    BI_RGB,
    COMCTL32_SIGNATURES,
    DARK_THEME_FOREGROUND,
    ERROR_CLASS_ALREADY_EXISTS,
    GDI32_SIGNATURES,
    KERNEL32_SIGNATURES,
    LABEL_BITS_PER_PIXEL,
    LIGHT_THEME_FOREGROUND,
    ULW_ALPHA,
    USER32_SIGNATURES,
    TTM_ADDTOOLW,
    TTM_SETMAXTIPWIDTH,
    TTM_SETTIPBKCOLOR,
    TTM_SETTIPTEXTCOLOR,
    TTM_TRACKACTIVATE,
    TTM_TRACKPOSITION,
    TTM_UPDATETIPTEXTW,
    UXTHEME_SIGNATURES,
    rgb_from_colorref,
    WM_PAINT,
    WM_QUIT,
    WM_SETTINGCHANGE,
    WS_CHILD,
    WS_EX_LAYERED,
    WS_POPUP,
    apply_signatures,
    foreground_color_for_theme,
)
from claudemonitor import label_art
from claudemonitor import win32_taskbar_window
from claudemonitor.win32_taskbar_window import (
    Win32TaskbarWindow,
    _bitmap_info_for,
    _ICON_CONTENT_RIGHT_PADDING,
    _ICON_LEFT_INSET,
    _ICON_SIZE,
    _ICON_TEXT_GAP,
    _STACK_VERTICAL_PADDING,
    _initialize_tooltip_controls,
)


# Windows' SWP_SHOWWINDOW bit. Repositioning must not pass it because that would
# override the user's separate visibility choice.
_WINDOW_FLAG_SHOW = 0x0040

# Windows' WS_VISIBLE bit, which create_window must never set.
_WINDOW_STYLE_VISIBLE = 0x10000000


class _FakeDll:
    """Record every native call, returning a benign success value by default."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.results: dict[str, object] = {}

    def __getattr__(self, name: str):
        def call(*args):
            self.calls.append((name, *args))
            result = self.results.get(name, 1)
            return result(*args) if callable(result) else result

        return call

    def named(self, name: str) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] == name]

    def was_called(self, name: str) -> bool:
        return bool(self.named(name))


class _FakeUser32(_FakeDll):
    """Emulate the window-management subset the taskbar label depends on."""

    def __init__(self) -> None:
        super().__init__()
        self.style = WS_POPUP
        self.extended_style = 0
        self.last_error = 0
        self.parent_calls: list[tuple[int, int]] = []
        self.parent_result = 99
        self.position_flags: list[int] = []
        # Handle -> (rect, visible). A handle absent from this map is treated as
        # a window that vanished between enumeration and measurement.
        self.windows: dict[int, tuple[Rect, bool]] = {}
        self.child_chain: list[int] = []
        self.queued_messages: list[int] = []
        # The taskbar the label is painted into: 200px wide, 48px tall, which
        # is the Windows 11 default at 100% scaling.
        self.client_rect = Rect(left=0, top=0, right=200, bottom=48)

    def GetClientRect(self, handle, rect_pointer):
        self.calls.append(("GetClientRect", handle))
        target = rect_pointer._obj
        target.left, target.top, target.right, target.bottom = (
            self.client_rect.left,
            self.client_rect.top,
            self.client_rect.right,
            self.client_rect.bottom,
        )
        return 1

    def GetWindowLongPtrW(self, handle, index):
        self.calls.append(("GetWindowLongPtrW", handle, index))
        return self.extended_style if index == win32_bindings.GWL_EXSTYLE else self.style

    def SetWindowLongPtrW(self, handle, index, value):
        self.calls.append(("SetWindowLongPtrW", handle, index, value))
        if index == win32_bindings.GWL_EXSTYLE:
            previous, self.extended_style = self.extended_style, value
        else:
            previous, self.style = self.style, value
        ctypes.set_last_error(self.last_error)
        return previous

    def SetParent(self, handle, taskbar):
        self.calls.append(("SetParent", handle, taskbar))
        self.parent_calls.append((handle, taskbar))
        ctypes.set_last_error(self.last_error)
        return self.parent_result

    def SetWindowPos(self, handle, insert_after, left, top, width, height, flags):
        self.calls.append(("SetWindowPos", handle, insert_after, left, top, width, height, flags))
        self.position_flags.append(flags)
        return 1

    def GetWindowRect(self, handle, rect_pointer):
        # ``rect_pointer`` is the CArgObject produced by ctypes.byref; ._obj is
        # the RECT the real API would fill in through that pointer.
        self.calls.append(("GetWindowRect", handle))
        known = self.windows.get(handle)
        if known is None:
            return 0
        rect = known[0]
        target = rect_pointer._obj
        target.left, target.top, target.right, target.bottom = (
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
        )
        return 1

    def IsWindowVisible(self, handle):
        self.calls.append(("IsWindowVisible", handle))
        known = self.windows.get(handle)
        return 1 if known and known[1] else 0

    def GetWindow(self, handle, relationship):
        self.calls.append(("GetWindow", handle, relationship))
        if relationship == win32_bindings.GW_CHILD:
            return self.child_chain[0] if self.child_chain else 0
        if handle in self.child_chain:
            position = self.child_chain.index(handle) + 1
            if position < len(self.child_chain):
                return self.child_chain[position]
        return 0

    def PeekMessageW(self, message_pointer, handle, first, last, flags):
        self.calls.append(("PeekMessageW",))
        if not self.queued_messages:
            return 0
        message_pointer._obj.message = self.queued_messages.pop(0)
        return 1


class _FakeGdi32(_FakeDll):
    """Hand out a real pixel buffer, so a paint can be read back and asserted on.

    CreateDIBSection is the one call whose return value is not enough: the label
    composes into the memory Windows allocates behind it, so a fake that only
    records the call would leave nothing to inspect.
    """

    def __init__(self) -> None:
        super().__init__()
        self.surfaces: list[tuple[ctypes.Array, int, int]] = []

    def CreateDIBSection(self, dc, info_pointer, usage, bits_pointer, mapping, offset):
        header = info_pointer._obj.bmiHeader
        width, height = header.biWidth, abs(header.biHeight)
        surface = ctypes.create_string_buffer(width * height * 4)
        self.surfaces.append((surface, width, height))
        bits_pointer._obj.value = ctypes.addressof(surface)
        self.calls.append(("CreateDIBSection", width, height, header.biBitCount))
        return self.results.get("CreateDIBSection", 77)

    def bitmap(self, index: int = -1) -> Image.Image:
        """Read one surface back the way Windows composites it: premultiplied BGRA."""
        surface, width, height = self.surfaces[index]
        return Image.frombuffer(
            "RGBA", (width, height), surface.raw, "raw", "BGRA", 0, 1
        )


def _client_area(user32: _FakeUser32) -> wintypes.RECT:
    """Return what GetClientRect would fill in, as the paint code receives it."""
    rect = user32.client_rect
    return wintypes.RECT(rect.left, rect.top, rect.right, rect.bottom)


def _window(
    *,
    user32: _FakeUser32 | None = None,
    gdi32: _FakeDll | None = None,
    uxtheme: _FakeDll | None = None,
):
    """Build a Win32TaskbarWindow whose DLLs are replaced by recording fakes."""
    native = Win32TaskbarWindow()
    native._user32 = user32 or _FakeUser32()
    native._gdi32 = gdi32 or _FakeGdi32()
    native._uxtheme = uxtheme or _FakeDll()
    return native


class TestSignatureTable:
    """Argument types are declared from a table so 64-bit handles never truncate."""

    def test_apply_signatures_declares_argument_and_return_types(self):
        dll = type("_Blank", (), {})()
        dll.FindWindowW = type("_Function", (), {})()

        apply_signatures(dll, {"FindWindowW": ((wintypes.LPCWSTR,), wintypes.HWND)})

        assert dll.FindWindowW.argtypes == (wintypes.LPCWSTR,)
        assert dll.FindWindowW.restype == wintypes.HWND

    def test_message_parameters_are_pointer_sized(self):
        # A 32-bit LPARAM would truncate pointers Windows passes to our callback.
        _argument_types, _return_type = USER32_SIGNATURES["DefWindowProcW"]

        assert ctypes.sizeof(_argument_types[3]) == ctypes.sizeof(ctypes.c_void_p)
        assert ctypes.sizeof(_return_type) == ctypes.sizeof(ctypes.c_void_p)

    def test_every_declared_function_exists_in_its_dll(self):
        native = Win32TaskbarWindow()
        tables = (
            (native._user32, USER32_SIGNATURES),
            (native._gdi32, GDI32_SIGNATURES),
            (native._kernel32, KERNEL32_SIGNATURES),
            (native._comctl32, COMCTL32_SIGNATURES),
            (native._uxtheme, UXTHEME_SIGNATURES),
        )

        for dll, signatures in tables:
            for name in signatures:
                assert getattr(dll, name).argtypes is not None

    def test_a_missing_export_is_skipped_rather_than_crashing_startup(self):
        # GetDpiForWindow-style exports do not exist on older Windows builds,
        # and an AttributeError here would abort the whole application.
        dll = type("_Blank", (), {})()
        dll.FindWindowW = type("_Function", (), {})()

        missing = apply_signatures(
            dll,
            {
                "FindWindowW": ((wintypes.LPCWSTR,), wintypes.HWND),
                "NotOnThisWindows": ((), wintypes.BOOL),
            },
        )

        assert missing == ["NotOnThisWindows"]
        assert dll.FindWindowW.restype == wintypes.HWND


class TestTaskbarAttachment:
    def test_attachment_converts_popup_style_to_child_style(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)

        assert native.attach_to_taskbar(30, 10) is True

        assert user32.parent_calls == [(30, 10)]
        assert user32.style & WS_CHILD
        assert user32.style & WS_POPUP == 0

    def test_rejected_attachment_restores_the_original_popup_style(self):
        # Explorer can refuse the child; the controller then needs a real popup
        # it can place in absolute screen coordinates.
        user32 = _FakeUser32()
        user32.parent_result = 0
        user32.last_error = 5  # ERROR_ACCESS_DENIED
        native = _window(user32=user32)

        assert native.attach_to_taskbar(30, 10) is False

        assert user32.style & WS_POPUP
        assert user32.style & WS_CHILD == 0

    def test_null_previous_parent_without_an_error_still_counts_as_success(self):
        user32 = _FakeUser32()
        user32.parent_result = 0
        user32.last_error = 0
        native = _window(user32=user32)

        assert native.attach_to_taskbar(30, 10) is True


class TestGeometry:
    def test_get_rect_reports_the_windows_screen_bounds(self):
        user32 = _FakeUser32()
        user32.windows = {30: (Rect(1, 2, 3, 4), True)}

        assert _window(user32=user32).get_rect(30) == Rect(1, 2, 3, 4)

    def test_get_rect_raises_when_windows_rejects_the_handle(self):
        with pytest.raises(OSError):
            _window(user32=_FakeUser32()).get_rect(30)

    def test_siblings_that_vanish_mid_enumeration_are_skipped(self):
        # Taskbar children are transient; one closing between GetWindow and
        # GetWindowRect must not abort placement for the whole session.
        user32 = _FakeUser32()
        user32.child_chain = [41, 42, 43]
        user32.windows = {
            41: (Rect(100, 0, 200, 40), True),
            # 42 is deliberately absent, i.e. already destroyed.
            43: (Rect(300, 0, 400, 40), True),
        }
        user32.results["IsWindowVisible"] = 1

        rects = _window(user32=user32).list_sibling_rects(taskbar=10, exclude_handle=30)

        assert rects == [Rect(100, 0, 200, 40), Rect(300, 0, 400, 40)]

    def test_hidden_and_empty_siblings_are_ignored(self):
        user32 = _FakeUser32()
        user32.child_chain = [41, 42, 43]
        user32.windows = {
            41: (Rect(100, 0, 200, 40), True),
            42: (Rect(0, 0, 0, 0), True),
            43: (Rect(300, 0, 400, 40), False),
        }

        rects = _window(user32=user32).list_sibling_rects(taskbar=10, exclude_handle=30)

        assert rects == [Rect(100, 0, 200, 40)]

    def test_our_own_window_is_never_treated_as_a_sibling(self):
        user32 = _FakeUser32()
        user32.child_chain = [30, 41]
        user32.windows = {
            30: (Rect(500, 0, 600, 40), True),
            41: (Rect(100, 0, 200, 40), True),
        }

        rects = _window(user32=user32).list_sibling_rects(taskbar=10, exclude_handle=30)

        assert rects == [Rect(100, 0, 200, 40)]

    def test_repositioning_does_not_force_a_hidden_window_visible(self):
        user32 = _FakeUser32()

        _window(user32=user32).move_window(30, Rect(1, 2, 101, 42), topmost=False)

        assert user32.position_flags[-1] & _WINDOW_FLAG_SHOW == 0


class TestWindowLifecycle:
    def test_the_window_is_created_hidden_so_it_can_be_placed_first(self):
        # WS_VISIBLE at creation would flash a 1x1 speck at the screen origin.
        user32 = _FakeUser32()

        _window(user32=user32).create_window(text="Claude: loading...")

        created = user32.named("CreateWindowExW")[0]
        style = created[4]
        assert style & WS_POPUP
        assert style & _WINDOW_STYLE_VISIBLE == 0

    def test_creation_failure_is_reported_rather_than_returning_a_null_handle(self):
        user32 = _FakeUser32()
        user32.results["CreateWindowExW"] = 0

        with pytest.raises(OSError):
            _window(user32=user32).create_window(text="Claude: loading...")

    def test_closing_destroys_a_window_that_still_exists(self):
        user32 = _FakeUser32()

        _window(user32=user32).close_window(30)

        assert ("DestroyWindow", 30) in user32.calls

    def test_closing_a_stale_handle_is_a_no_op(self):
        # Explorer may already have destroyed the child during a restart.
        user32 = _FakeUser32()
        user32.results["IsWindow"] = 0

        _window(user32=user32).close_window(30)

        assert not user32.was_called("DestroyWindow")


class TestTooltip:
    """Hovering the taskbar label exposes the same detail as the tray icon."""

    def test_common_controls_are_initialized_for_the_tooltip_window_class(self):
        comctl32 = _FakeDll()

        _initialize_tooltip_controls(comctl32)

        initialized = comctl32.named("InitCommonControlsEx")
        assert initialized
        assert initialized[0][1]._obj.dwSize > 0
        assert initialized[0][1]._obj.dwICC != 0

    def test_failed_common_control_initialization_is_reported(self):
        comctl32 = _FakeDll()
        comctl32.results["InitCommonControlsEx"] = 0

        with pytest.raises(OSError):
            _initialize_tooltip_controls(comctl32)

    def test_cursor_inside_the_label_rectangle_opens_its_tooltip(self):
        user32 = _FakeUser32()
        user32.windows[30] = (Rect(100, 200, 300, 240), True)

        def point_inside(point_pointer):
            point_pointer._obj.x = 150
            point_pointer._obj.y = 220
            return 1

        user32.results["GetCursorPos"] = point_inside
        user32.windows[41] = (Rect(0, 0, 180, 70), True)
        native = _window(user32=user32)
        native._tooltip_handles[30] = 41

        native._poll_tooltip_hover()

        assert any(
            call[1:3] == (41, TTM_TRACKPOSITION)
            for call in user32.named("SendMessageW")
        )
        assert any(
            call[1:4] == (41, TTM_TRACKACTIVATE, True)
            for call in user32.named("SendMessageW")
        )
        tooltip_position = user32.named("SetWindowPos")[-1]
        assert tooltip_position[1:7] == (41, -1, 110, 126, 0, 0)
        assert tooltip_position[7] & win32_bindings.SWP_NOSIZE
        assert tooltip_position[7] & win32_bindings.SWP_NOACTIVATE
        assert 30 in native._active_tooltip_labels

    def test_cursor_polling_does_not_reopen_and_flicker_an_active_tooltip(self):
        user32 = _FakeUser32()
        user32.windows[30] = (Rect(100, 200, 300, 240), True)

        def point_inside(point_pointer):
            point_pointer._obj.x = 150
            point_pointer._obj.y = 220
            return 1

        user32.results["GetCursorPos"] = point_inside
        user32.windows[41] = (Rect(0, 0, 180, 70), True)
        native = _window(user32=user32)
        native._tooltip_handles[30] = 41

        native._poll_tooltip_hover()
        native._poll_tooltip_hover()

        popup_calls = [
            call
            for call in user32.named("SendMessageW")
            if call[2] == TTM_TRACKACTIVATE and call[3] is True
        ]
        assert len(popup_calls) == 1

    def test_cursor_leaving_the_label_rectangle_closes_its_tooltip(self):
        user32 = _FakeUser32()
        user32.windows[30] = (Rect(100, 200, 300, 240), True)
        pointer = [150, 220]

        def fill_point(point_pointer):
            point_pointer._obj.x, point_pointer._obj.y = pointer
            return 1

        user32.results["GetCursorPos"] = fill_point
        native = _window(user32=user32)
        native._tooltip_handles[30] = 41
        native._poll_tooltip_hover()

        pointer[:] = [50, 50]
        native._poll_tooltip_hover()

        assert any(
            call[1:4] == (41, TTM_TRACKACTIVATE, False)
            for call in user32.named("SendMessageW")
        )
        assert 30 not in native._active_tooltip_labels

    def test_setting_a_tooltip_attaches_a_multiline_native_tooltip_to_the_label(self):
        user32 = _FakeUser32()
        uxtheme = _FakeDll()
        uxtheme.results["SetWindowTheme"] = 0
        native = _window(user32=user32, uxtheme=uxtheme)
        native._uses_light_theme = False
        detail = "Claude usage\n5h: 89% left · resets in 1h 28m\nWeek: 85% left"

        native.set_tooltip(30, detail)

        created = user32.named("CreateWindowExW")
        assert any(call[2] == "tooltips_class32" for call in created)
        assert any(call[2] == TTM_SETMAXTIPWIDTH for call in user32.named("SendMessageW"))
        assert any(call[2] == TTM_ADDTOOLW for call in user32.named("SendMessageW"))
        assert uxtheme.named("SetWindowTheme") == [
            ("SetWindowTheme", 1, "DarkMode_Explorer", None)
        ]
        assert not any(
            call[2] in (TTM_SETTIPBKCOLOR, TTM_SETTIPTEXTCOLOR)
            for call in user32.named("SendMessageW")
        )
        assert native._tooltip_buffers[30].value == detail

    def test_light_taskbar_uses_windows_explorer_tooltip_theme(self):
        user32 = _FakeUser32()
        uxtheme = _FakeDll()
        uxtheme.results["SetWindowTheme"] = 0
        native = _window(user32=user32, uxtheme=uxtheme)
        native._uses_light_theme = True

        native.set_tooltip(30, "detail")

        assert uxtheme.named("SetWindowTheme") == [
            ("SetWindowTheme", 1, "Explorer", None)
        ]

    def test_dark_color_fallback_is_used_when_explorer_theme_is_unavailable(self):
        user32 = _FakeUser32()
        uxtheme = _FakeDll()
        uxtheme.results["SetWindowTheme"] = 1
        native = _window(user32=user32, uxtheme=uxtheme)
        native._uses_light_theme = False

        native.set_tooltip(30, "detail")

        assert uxtheme.named("SetWindowTheme") == [
            ("SetWindowTheme", 1, "DarkMode_Explorer", None),
            ("SetWindowTheme", 1, "", ""),
        ]
        assert any(call[2] == TTM_SETTIPBKCOLOR for call in user32.named("SendMessageW"))
        assert any(call[2] == TTM_SETTIPTEXTCOLOR for call in user32.named("SendMessageW"))

    def test_updating_tooltip_text_reuses_the_existing_native_control(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.set_tooltip(30, "old detail")

        native.set_tooltip(30, "new detail")

        created_classes = [call[2] for call in user32.named("CreateWindowExW")]
        assert created_classes.count("tooltips_class32") == 1
        assert any(call[2] == TTM_UPDATETIPTEXTW for call in user32.named("SendMessageW"))
        assert native._tooltip_buffers[30].value == "new detail"

    def test_visible_tooltip_text_updates_without_reopening_the_popup(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.set_tooltip(30, "Updated (1 second ago)")
        native._active_tooltip_labels.add(30)

        native.set_tooltip(30, "Updated (2 seconds ago)")

        updates = [
            call
            for call in user32.named("SendMessageW")
            if call[2] == TTM_UPDATETIPTEXTW
        ]
        redraws = [
            call
            for call in user32.named("SendMessageW")
            if call[2] == win32_bindings.WM_USER + 29
        ]
        assert len(updates) == 1
        assert len(redraws) == 1
        assert native._tooltip_buffers[30].value == "Updated (2 seconds ago)"
        assert not any(
            call[2] == TTM_TRACKACTIVATE
            for call in user32.named("SendMessageW")
        )

    def test_closing_the_label_also_destroys_its_tooltip_control(self):
        user32 = _FakeUser32()
        user32.results["CreateWindowExW"] = 41
        native = _window(user32=user32)
        native.set_tooltip(30, "detail")

        native.close_window(30)

        destroyed = user32.named("DestroyWindow")
        assert destroyed[:2] == [
            ("DestroyWindow", 41),
            ("DestroyWindow", 30),
        ]


class TestStyleWrites:
    def test_layered_transparency_preserves_existing_extended_styles(self):
        user32 = _FakeUser32()
        user32.extended_style = win32_bindings.WS_EX_TOOLWINDOW

        _window(user32=user32).enable_per_pixel_alpha(30)

        assert user32.extended_style & win32_bindings.WS_EX_TOOLWINDOW
        assert user32.extended_style & WS_EX_LAYERED

    def test_a_failed_style_write_is_reported_rather_than_ignored(self):
        user32 = _FakeUser32()
        user32.extended_style = 0
        user32.last_error = 5

        with pytest.raises(OSError):
            _window(user32=user32).enable_per_pixel_alpha(30)

    def test_no_colour_is_nominated_as_transparent(self):
        # A window told to treat one colour as a hole cannot also carry an alpha
        # channel: Windows accepts whichever was configured last and ignores the
        # other. Every edge in this label depends on the alpha channel winning.
        user32 = _FakeUser32()

        _window(user32=user32).enable_per_pixel_alpha(30)

        assert not user32.was_called("SetLayeredWindowAttributes")


class TestTheme:
    """Near-white text is unreadable on a Windows 11 light-mode taskbar."""

    def test_dark_theme_uses_a_near_white_foreground(self):
        assert foreground_color_for_theme(uses_light_theme=False) == DARK_THEME_FOREGROUND

    def test_light_theme_uses_a_near_black_foreground(self):
        assert foreground_color_for_theme(uses_light_theme=True) == LIGHT_THEME_FOREGROUND

    def test_text_is_painted_in_the_colour_the_active_theme_calls_for(
        self, monkeypatch
    ):
        # GDI now rasterises coverage rather than colour, so the theme reaches
        # the picture when that coverage is painted rather than at DrawTextW.
        monkeypatch.setattr(win32_taskbar_window, "system_uses_light_theme", lambda: True)
        native = _window()
        native.refresh_theme(30)

        covered = native._text_picture(Image.new("L", (1, 1), 255))

        assert covered.getpixel((0, 0)) == rgb_from_colorref(LIGHT_THEME_FOREGROUND) + (
            255,
        )

    def test_gdi_is_asked_for_coverage_rather_than_for_the_final_colour(self):
        # Drawing the text in its own colour would leave every letter edge faded
        # towards black, which is the background it was rasterised against.
        gdi32 = _FakeGdi32()
        native = _window(gdi32=gdi32)
        native.set_segments(30, [LabelSegment(CLAUDE, "80%")])

        native._window_proc(30, WM_PAINT, 0, 0)

        colours = {call[2] for call in gdi32.named("SetTextColor")}
        assert colours == {win32_taskbar_window._TEXT_COVERAGE_COLOR}

    def test_refreshing_after_a_theme_switch_repaints_with_the_new_colour(
        self, monkeypatch
    ):
        # A taskbar child never receives WM_SETTINGCHANGE, so the controller
        # polls this method instead of relying on the broadcast message.
        theme_is_light = [False]
        monkeypatch.setattr(
            win32_taskbar_window,
            "system_uses_light_theme",
            lambda: theme_is_light[0],
        )
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.refresh_theme(30)

        theme_is_light[0] = True
        native.refresh_theme(30)

        assert user32.was_called("InvalidateRect")
        assert native._foreground_rgb() == rgb_from_colorref(LIGHT_THEME_FOREGROUND)

    def test_an_unchanged_theme_does_not_force_a_repaint(self, monkeypatch):
        # Polling once a second must not invalidate the window every time.
        monkeypatch.setattr(win32_taskbar_window, "system_uses_light_theme", lambda: False)
        user32 = _FakeUser32()
        native = _window(user32=user32)

        native.refresh_theme(30)
        native.refresh_theme(30)

        assert not user32.was_called("InvalidateRect")

    def test_a_theme_change_message_still_repaints_the_fallback_popup(self, monkeypatch):
        theme_is_light = [False]
        monkeypatch.setattr(
            win32_taskbar_window,
            "system_uses_light_theme",
            lambda: theme_is_light[0],
        )
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.refresh_theme(30)

        theme_is_light[0] = True
        native._window_proc(30, WM_SETTINGCHANGE, 0, 0)

        assert user32.was_called("InvalidateRect")


class TestPainting:
    def test_current_text_is_drawn_on_every_paint_request(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80% (3h 0m)")]

        native._window_proc(30, WM_PAINT, 0, 0)

        drawn = user32.named("DrawTextW")
        assert drawn and drawn[0][2] == "80% (3h 0m)"

    def test_the_finished_picture_is_handed_to_windows_to_composite(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80% (3h 0m)")]

        native._window_proc(30, WM_PAINT, 0, 0)

        assert len(user32.named("UpdateLayeredWindow")) == 1

    def test_windows_is_told_to_read_the_alpha_channel(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80%")]

        native._window_proc(30, WM_PAINT, 0, 0)

        pushed = user32.named("UpdateLayeredWindow")[0]
        blend = pushed[8]._obj
        assert pushed[9] == ULW_ALPHA
        assert blend.AlphaFormat == AC_SRC_ALPHA
        assert blend.SourceConstantAlpha == 255

    def test_the_bitmap_covers_the_whole_client_area(self):
        user32 = _FakeUser32()
        user32.client_rect = Rect(left=0, top=0, right=137, bottom=60)
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80%")]

        native._window_proc(30, WM_PAINT, 0, 0)

        size = user32.named("UpdateLayeredWindow")[0][4]._obj
        assert (size.cx, size.cy) == (137, 60)

    def test_the_text_rect_leaves_room_for_the_icon_on_the_left(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80% (3h 0m)")]

        native._window_proc(30, WM_PAINT, 0, 0)

        drawn_rect = user32.named("DrawTextW")[0][4]._obj
        assert drawn_rect.left > 0

    def test_every_drawing_surface_is_released(self):
        # This runs once a second for as long as the machine is on, so a handle
        # left behind is a leak that eventually exhausts the GDI quota.
        user32 = _FakeUser32()
        gdi32 = _FakeGdi32()
        native = _window(user32=user32, gdi32=gdi32)
        native._segments = [LabelSegment(CLAUDE, "80%")]

        native._window_proc(30, WM_PAINT, 0, 0)

        assert len(gdi32.named("DeleteObject")) == len(gdi32.named("CreateDIBSection"))
        assert len(gdi32.named("DeleteDC")) == len(gdi32.named("CreateCompatibleDC"))
        assert len(user32.named("ReleaseDC")) == len(user32.named("GetDC"))

    def test_a_surface_windows_refuses_is_reported_and_released(self):
        user32 = _FakeUser32()
        gdi32 = _FakeGdi32()
        gdi32.results["CreateDIBSection"] = 0
        native = _window(user32=user32, gdi32=gdi32)
        native._segments = [LabelSegment(CLAUDE, "80%")]

        native._window_proc(30, WM_PAINT, 0, 0)

        assert not user32.was_called("UpdateLayeredWindow")
        assert len(gdi32.named("DeleteDC")) == 1
        assert len(user32.named("ReleaseDC")) == 1

    def test_a_label_with_no_area_yet_is_not_painted(self):
        # A window is created 1x1 and only sized once the taskbar has been
        # measured, and a zero-sized bitmap is not something Windows accepts.
        user32 = _FakeUser32()
        user32.client_rect = Rect(left=0, top=0, right=0, bottom=0)
        native = _window(user32=user32)
        native._segments = [LabelSegment(CLAUDE, "80%")]

        native._window_proc(30, WM_PAINT, 0, 0)

        assert not user32.was_called("UpdateLayeredWindow")

    def test_unhandled_messages_are_delegated_to_windows(self):
        user32 = _FakeUser32()
        user32.results["DefWindowProcW"] = 7
        unhandled_message = 0x0005  # WM_SIZE

        result = _window(user32=user32)._window_proc(30, unhandled_message, 1, 2)

        assert result == 7
        assert ("DefWindowProcW", 30, unhandled_message, 1, 2) in user32.calls


class TestAntiAliasedEdges:
    """The complaint this rendering answers: both marks were ringed in black.

    Each mark is thin enough that almost every one of its pixels is a partly
    covered edge, and flattening those onto the colour the label was erased with
    turned the whole mark towards that colour.
    """

    def _painted_bitmap(self, segments) -> Image.Image:
        gdi32 = _FakeGdi32()
        native = _window(gdi32=gdi32)
        native.set_segments(30, segments)
        native._window_proc(30, WM_PAINT, 0, 0)
        return gdi32.bitmap()

    def test_a_marks_edge_reaches_windows_partly_transparent(self):
        bitmap = self._painted_bitmap([LabelSegment(CLAUDE, "80%")])

        alphas = {pixel[3] for pixel in bitmap.get_flattened_data()}
        assert alphas & set(range(1, 255))

    def test_nothing_outside_the_mark_carries_any_colour(self):
        # A pixel the mark does not cover has to be transparent *and* black.
        # Colour left behind at zero alpha is what a premultiplied surface
        # renders as a halo.
        bitmap = self._painted_bitmap([LabelSegment(CLAUDE, "80%")])

        assert not [
            pixel
            for pixel in bitmap.get_flattened_data()
            if pixel[3] == 0 and any(pixel[:3])
        ]

    def test_no_pixel_is_brighter_than_its_own_alpha_allows(self):
        # Windows reads this surface as premultiplied. A channel above the alpha
        # it is paired with is the definition of an over-bright edge.
        bitmap = self._painted_bitmap([LabelSegment(CLAUDE, "80%")])

        assert all(
            max(pixel[:3]) <= pixel[3] for pixel in bitmap.get_flattened_data()
        )

    def test_the_bitmap_is_a_top_down_32bpp_surface(self):
        info = _bitmap_info_for(width=16, height=16)

        assert info.bmiHeader.biWidth == 16
        # Negative height marks the DIB top-down, matching row 0 = top row.
        assert info.bmiHeader.biHeight == -16
        assert info.bmiHeader.biBitCount == LABEL_BITS_PER_PIXEL
        assert info.bmiHeader.biPlanes == 1
        assert info.bmiHeader.biCompression == BI_RGB

    def test_the_text_is_flushed_before_its_coverage_is_read_back(self):
        # GDI batches its drawing. Reading a DIB section it was told to draw
        # into, without flushing first, can return a surface the text has not
        # reached yet, giving a label that shows its marks alone.
        gdi32 = _FakeGdi32()
        native = _window(gdi32=gdi32)
        native.set_segments(30, [LabelSegment(CLAUDE, "80%")])

        native._window_proc(30, WM_PAINT, 0, 0)

        assert gdi32.was_called("GdiFlush")

    def test_the_font_is_asked_for_grey_anti_aliasing(self):
        # ClearType tints each edge for one assumed background colour, which is
        # exactly the assumption a per-pixel alpha window cannot make.
        gdi32 = _FakeGdi32()
        native = _window(gdi32=gdi32)

        native.message_font(96)

        logfont = gdi32.named("CreateFontIndirectW")[0][1]._obj
        assert logfont.lfQuality == ANTIALIASED_QUALITY


class TestTextMeasurement:
    """The label is sized to fit exactly the icon plus the current text, so a
    short string like an error label never leaves dead space before the clock."""

    def _fake_gdi32_reporting_width(self, width: int) -> _FakeDll:
        gdi32 = _FakeDll()

        def fill_size(hdc, text, length, size_pointer):
            size_pointer._obj.cx = width
            size_pointer._obj.cy = 15
            return 1

        gdi32.results["GetTextExtentPoint32W"] = fill_size
        return gdi32

    def test_measured_width_matches_what_windows_reports(self):
        gdi32 = self._fake_gdi32_reporting_width(91)

        assert _window(gdi32=gdi32).measure_text_width("95% (4h 14m)") == 91

    def test_the_measurement_device_context_is_always_released(self):
        gdi32 = _FakeDll()

        _window(gdi32=gdi32).measure_text_width("x")

        assert gdi32.was_called("CreateCompatibleDC")
        assert gdi32.was_called("DeleteDC")

    def test_content_width_adds_the_icon_prefix_and_right_padding(self):
        gdi32 = self._fake_gdi32_reporting_width(100)

        content_width = _window(gdi32=gdi32).content_width_for(
            [LabelSegment(CLAUDE, "anything")]
        )

        assert content_width == (
            _ICON_LEFT_INSET + _ICON_SIZE + _ICON_TEXT_GAP + 100 + _ICON_CONTENT_RIGHT_PADDING
        )

    def test_shorter_text_yields_a_narrower_content_width(self):
        native = _window(gdi32=self._fake_gdi32_reporting_width(40))
        wider_native = _window(gdi32=self._fake_gdi32_reporting_width(120))

        assert native.content_width_for(
            [LabelSegment(CLAUDE, "40%")]
        ) < wider_native.content_width_for(
            [LabelSegment(CLAUDE, "100% (not started)")]
        )


class TestFont:
    def test_the_system_message_font_is_preferred_over_the_stock_font(self):
        gdi32 = _FakeDll()
        gdi32.results["CreateFontIndirectW"] = 55

        assert _window(gdi32=gdi32).message_font() == 55

    def test_a_failed_metrics_query_falls_back_to_the_stock_ui_font(self):
        user32 = _FakeUser32()
        # Both the per-DPI query and the legacy one have to fail before the
        # stock font is the only option left.
        user32.results["SystemParametersInfoForDpi"] = 0
        user32.results["SystemParametersInfoW"] = 0
        gdi32 = _FakeDll()
        gdi32.results["GetStockObject"] = 66

        assert _window(user32=user32, gdi32=gdi32).message_font() == 66

    def test_the_font_is_created_once_and_reused_for_later_paints(self):
        gdi32 = _FakeDll()
        native = _window(gdi32=gdi32)

        native.message_font()
        native.message_font()

        assert len(gdi32.named("CreateFontIndirectW")) == 1

    def test_a_null_font_is_not_re_requested_on_every_paint(self):
        # ctypes turns a NULL handle into None, which a "is not None" cache
        # check would mistake for "not resolved yet" on every WM_PAINT.
        gdi32 = _FakeDll()
        gdi32.results["CreateFontIndirectW"] = None
        gdi32.results["GetStockObject"] = None
        native = _window(gdi32=gdi32)

        native.message_font()
        native.message_font()

        assert len(gdi32.named("CreateFontIndirectW")) == 1


class TestClassRegistration:
    def test_an_already_registered_class_is_not_an_error(self):
        # A second Win32TaskbarWindow in the same process must not crash.
        user32 = _FakeUser32()
        user32.results["RegisterClassExW"] = 0
        native = _window(user32=user32)

        def already_registered(*_args):
            user32.calls.append(("RegisterClassExW",))
            ctypes.set_last_error(ERROR_CLASS_ALREADY_EXISTS)
            return 0

        user32.RegisterClassExW = already_registered

        native._register_class()

        assert user32.was_called("RegisterClassExW")

    def test_a_genuine_registration_failure_still_raises(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)

        def rejected(*_args):
            ctypes.set_last_error(5)
            return 0

        user32.RegisterClassExW = rejected

        with pytest.raises(OSError):
            native._register_class()

    def test_a_successful_registration_keeps_its_callback_referenced(self):
        # Windows may call this class's window procedure for the rest of the
        # process's life, so the registering instance's callback must outlive
        # the instance itself rather than being freed once it is discarded.
        native = _window(user32=_FakeUser32())

        native._register_class()

        assert native._window_proc_callback in win32_taskbar_window._registered_wndproc_callbacks

    def test_an_already_registered_class_does_not_add_a_duplicate_reference(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)

        def already_registered(*_args):
            ctypes.set_last_error(ERROR_CLASS_ALREADY_EXISTS)
            return 0

        user32.RegisterClassExW = already_registered
        before = len(win32_taskbar_window._registered_wndproc_callbacks)

        native._register_class()

        assert len(win32_taskbar_window._registered_wndproc_callbacks) == before


class TestMessagePump:
    def test_a_quit_message_stops_the_companion_thread(self):
        user32 = _FakeUser32()
        user32.queued_messages = [WM_QUIT]
        stop_requested = threading.Event()

        _window(user32=user32).pump_messages(stop_requested, duration_seconds=5.0)

        assert stop_requested.is_set()
        assert not user32.was_called("DispatchMessageW")

    def test_queued_messages_are_dispatched_to_the_window_procedure(self):
        user32 = _FakeUser32()
        user32.queued_messages = [WM_PAINT]
        stop_requested = threading.Event()

        _window(user32=user32).pump_messages(stop_requested, duration_seconds=0.01)

        assert user32.was_called("DispatchMessageW")
        assert not stop_requested.is_set()

    def test_a_shutdown_request_returns_immediately(self):
        user32 = _FakeUser32()
        stop_requested = threading.Event()
        stop_requested.set()

        _window(user32=user32).pump_messages(stop_requested, duration_seconds=30.0)

        assert not user32.was_called("PeekMessageW")


# ===========================================================================
# Two providers on one label.
# ===========================================================================


def _claude(text: str) -> LabelSegment:
    return LabelSegment(provider=CLAUDE, text=text)


def _codex(text: str) -> LabelSegment:
    return LabelSegment(provider=CODEX, text=text)


class TestSegmentWidth:
    """Stacked segments share one width: the widest row's."""

    def test_a_second_row_of_the_same_length_costs_no_width(self):
        native = _window()

        assert native.content_width_for(
            [_claude("80%"), _codex("64%")]
        ) == native.content_width_for([_claude("80%")])

    def test_the_widest_row_sets_the_width(self):
        native = _window()

        assert native.content_width_for(
            [_claude("80%"), _codex("64% (2h 14m)")]
        ) == native.content_width_for([_codex("64% (2h 14m)")])

    def test_a_single_segment_keeps_the_original_layout(self):
        # One provider must measure exactly as it did before segments existed,
        # so adding Codex cannot shift the Claude-only label by a pixel.
        native = _window()

        assert native.content_width_for([_claude("80%")]) == (
            _ICON_LEFT_INSET
            + _ICON_SIZE
            + _ICON_TEXT_GAP
            + native.measure_text_width("80%")
            + _ICON_CONTENT_RIGHT_PADDING
        )

    def test_every_row_pays_for_its_own_glyph(self):
        # The glyph sits inside the row, so a row's width still covers it.
        native = _window()

        assert native.content_width_for([_codex("64% (2h 14m)")]) == (
            _ICON_LEFT_INSET
            + _ICON_SIZE
            + _ICON_TEXT_GAP
            + native.measure_text_width("64% (2h 14m)")
            + _ICON_CONTENT_RIGHT_PADDING
        )

    def test_an_empty_label_still_has_its_trailing_padding(self):
        assert _window().content_width_for([]) == _ICON_CONTENT_RIGHT_PADDING


class TestSetSegments:
    """set_segments replaces what the next paint will draw."""

    def test_stored_segments_drive_the_next_paint(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)

        native.set_segments(30, [_claude("80%"), _codex("64%")])
        native._window_proc(30, WM_PAINT, 0, 0)

        drawn = [call[2] for call in user32.named("DrawTextW")]
        assert drawn == ["80%", "64%"]

    def test_the_window_title_summarizes_every_segment(self):
        # Screen readers and Explorer only ever see the native title, so it has
        # to carry both providers rather than just the first.
        user32 = _FakeUser32()
        native = _window(user32=user32)

        native.set_segments(30, [_claude("80%"), _codex("64%")])

        title = user32.named("SetWindowTextW")[0][2]
        assert "80%" in title and "64%" in title

    def test_a_repaint_is_requested(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)

        native.set_segments(30, [_claude("80%")])

        assert user32.was_called("InvalidateRect")

    def test_a_failed_title_update_is_reported(self):
        user32 = _FakeUser32()
        user32.results["SetWindowTextW"] = 0
        native = _window(user32=user32)

        with pytest.raises(OSError):
            native.set_segments(30, [_claude("80%")])


class TestSegmentPainting:
    """Each segment draws its own provider glyph before its own text."""

    def _placed(self, segments, client_rect=None):
        """Draw the segments and return the marks the paint would lay over them."""
        user32 = _FakeUser32()
        if client_rect is not None:
            user32.client_rect = client_rect
        native = _window(user32=user32)
        native.set_segments(30, segments)
        rect = _client_area(user32)
        return user32, native._draw_segments(1, rect, 96)

    def test_one_mark_is_placed_per_segment(self):
        _user32, placements = self._placed([_claude("80%"), _codex("64%")])

        assert len(placements) == 2

    def test_segments_are_stacked_top_to_bottom(self):
        user32, _placements = self._placed([_claude("80%"), _codex("64%")])

        rects = [call[4]._obj for call in user32.named("DrawTextW")]
        assert rects[0].left == rects[1].left
        assert rects[0].top < rects[1].top

    def test_each_provider_supplies_its_own_mark(self):
        _user32, placements = self._placed([_claude("80%"), _codex("64%")])

        claude_mark, codex_mark = (glyph for glyph, _position in placements)
        assert claude_mark.tobytes() != codex_mark.tobytes()

    def test_each_row_is_drawn_with_its_own_providers_mark(self):
        # The segment carries its provider, so a paint can no longer be handed
        # a name nobody drew and have to guess which mark was meant.
        _user32, placements = self._placed([_claude("80%"), _codex("64%")])

        claude_mark, codex_mark = (glyph for glyph, _position in placements)
        assert claude_mark.tobytes() != codex_mark.tobytes()

    def test_a_taskbar_too_short_for_the_padding_still_draws_both_rows(self):
        # A taskbar set to small buttons leaves nothing to inset by. Cramped
        # beats a label that quietly stops showing one of its providers.
        user32, placements = self._placed(
            [_claude("80%"), _codex("64%")],
            client_rect=Rect(left=0, top=0, right=200, bottom=2),
        )

        assert len(user32.named("DrawTextW")) == 2
        for glyph, _position in placements:
            assert glyph.height <= 1


class TestThemedGlyphs:
    """The Codex mark is monochrome, so it follows the theme like the text does."""

    def _mark(self, segment):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.set_segments(30, [segment])
        rect = _client_area(user32)
        (glyph, _position), = native._draw_segments(1, rect, 96)
        return glyph

    def test_painting_uses_the_theme_the_label_last_read(self, monkeypatch):
        monkeypatch.setattr(
            win32_taskbar_window, "system_uses_light_theme", lambda: True
        )
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.refresh_theme(30)
        native.set_segments(30, [_codex("64%")])

        rect = _client_area(user32)
        (drawn, _position), = native._draw_segments(1, rect, 96)

        assert native._uses_light_theme is True
        assert drawn.tobytes() == label_art.taskbar_glyph(
            CODEX, drawn.width, uses_light_theme=True
        ).tobytes()

    def test_a_theme_switch_changes_the_mark_that_gets_drawn(self, monkeypatch):
        theme_is_light = [False]
        monkeypatch.setattr(
            win32_taskbar_window,
            "system_uses_light_theme",
            lambda: theme_is_light[0],
        )
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.refresh_theme(30)
        native.set_segments(30, [_codex("64%")])
        rect = _client_area(user32)
        (before, _position), = native._draw_segments(1, rect, 96)

        theme_is_light[0] = True
        native.refresh_theme(30)
        (after, _position), = native._draw_segments(1, rect, 96)

        assert before.tobytes() != after.tobytes()

    def test_an_empty_label_paints_without_error(self):
        user32 = _FakeUser32()
        native = _window(user32=user32)
        native.set_segments(30, [])

        native._window_proc(30, WM_PAINT, 0, 0)

        assert user32.named("DrawTextW") == []
        assert len(user32.named("UpdateLayeredWindow")) == 1


class TestStackedRows:
    """Two providers read as two lines, not as one long strip of numbers — and
    the stack keeps clear of the taskbar's own top and bottom edges."""

    def _painted(self, segments, client_rect=None):
        """Paint the segments and return the text rects and the marks placed."""
        user32 = _FakeUser32()
        if client_rect is not None:
            user32.client_rect = client_rect
        native = _window(user32=user32)
        native.set_segments(30, segments)
        rect = _client_area(user32)
        placements = native._draw_segments(1, rect, 96)
        rects = [call[4]._obj for call in user32.named("DrawTextW")]
        return rects, placements

    def test_each_row_gets_a_share_of_what_the_padding_leaves(self):
        rects, _placements = self._painted([_claude("80%"), _codex("64%")])

        first, second = rects
        assert (first.top, first.bottom) == (_STACK_VERTICAL_PADDING, 24)
        assert (second.top, second.bottom) == (24, 48 - _STACK_VERTICAL_PADDING)

    def test_the_stack_never_touches_the_taskbar_edges(self):
        # The reason for this whole layout: pressed against both edges, the
        # label read as a misplaced window rather than as part of the shell.
        rects, placements = self._painted([_claude("80%"), _codex("64%")])

        assert rects[0].top >= _STACK_VERTICAL_PADDING
        assert rects[-1].bottom <= 48 - _STACK_VERTICAL_PADDING
        for glyph, (_left, top) in placements:
            assert top >= _STACK_VERTICAL_PADDING
            assert top + glyph.height <= 48 - _STACK_VERTICAL_PADDING

    def test_the_marks_line_up_in_one_column(self):
        _rects, placements = self._painted([_claude("80%"), _codex("64%")])

        assert placements[0][1][0] == placements[1][1][0]

    def test_the_lower_mark_sits_in_the_lower_row(self):
        _rects, placements = self._painted([_claude("80%"), _codex("64%")])

        assert placements[0][1][1] < placements[1][1][1]

    def test_a_shared_row_shrinks_the_mark_below_its_nominal_size(self):
        # Scaled by the display alone, the mark grew to fill its half of the
        # taskbar completely, leaving a pixel of clearance above and below.
        _rects, placements = self._painted([_claude("80%"), _codex("64%")])

        for glyph, _position in placements:
            assert glyph.height < _ICON_SIZE

    def test_a_single_provider_keeps_the_full_sized_mark(self):
        _rects, placements = self._painted([_claude("80%")])

        assert placements[0][0].height == _ICON_SIZE

    def test_a_mark_never_outgrows_its_row(self):
        # A short taskbar split in two leaves less than the nominal 16px, and a
        # mark drawn at full size would spill into the row above and below.
        _rects, placements = self._painted(
            [_claude("80%"), _codex("64%")],
            client_rect=Rect(left=0, top=0, right=200, bottom=24),
        )

        for glyph, _position in placements:
            assert glyph.height <= 12

    def test_one_provider_still_gets_the_whole_stack(self):
        rects, _placements = self._painted([_claude("80%")])

        only = rects[0]
        assert (only.top, only.bottom) == (
            _STACK_VERTICAL_PADDING,
            48 - _STACK_VERTICAL_PADDING,
        )

    def test_an_empty_label_paints_nothing(self):
        rects, placements = self._painted([])

        assert rects == []
        assert placements == []
