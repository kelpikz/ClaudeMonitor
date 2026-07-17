from __future__ import annotations

import ctypes
from ctypes import wintypes

from claudemonitor.taskbar_companion import (
    HELLO_WORLD_TEXT,
    Rect,
    TaskbarCompanion,
    _WS_EX_NOACTIVATE,
    _WS_EX_TOOLWINDOW,
    _WS_POPUP,
    _WS_VISIBLE,
    leftmost_abutting_edge,
    taskbar_child_rect,
    Win32TaskbarWindow,
)


def test_loading_label_is_the_initial_native_window_content():
    assert HELLO_WORLD_TEXT == "Claude: loading..."


def test_leftmost_abutting_edge_steps_over_other_taskbar_plugins():
    # A TrafficMonitor-style plugin already occupies 1440..1594 right before
    # the notification area, so the companion must anchor left of it.
    children = [Rect(1440, 1032, 1594, 1080)]

    assert leftmost_abutting_edge(1594, children) == 1440


def test_leftmost_abutting_edge_chains_across_several_abutting_plugins():
    children = [
        Rect(1440, 1032, 1594, 1080),
        Rect(1300, 1032, 1445, 1080),
        Rect(200, 1032, 400, 1080),
    ]

    assert leftmost_abutting_edge(1594, children) == 1300


def test_leftmost_abutting_edge_ignores_windows_that_do_not_touch_the_anchor():
    children = [Rect(200, 1032, 400, 1080), Rect(900, 1032, 1100, 1080)]

    assert leftmost_abutting_edge(1594, children) == 1594


def test_taskbar_child_sits_immediately_before_notification_area():
    taskbar = Rect(left=0, top=1032, right=1920, bottom=1080)
    notification = Rect(left=1542, top=1032, right=1920, bottom=1080)

    assert taskbar_child_rect(taskbar, notification, width=180) == Rect(
        left=1362,
        top=0,
        right=1542,
        bottom=48,
    )


class _FakeNativeWindow:
    def __init__(self, *, pump_rounds: int = 2, attach_succeeds: bool = True):
        self.calls: list[tuple[object, ...]] = []
        self.pump_rounds = pump_rounds
        self.attach_succeeds = attach_succeeds
        self.notification_rect = Rect(1542, 1032, 1920, 1080)
        self.sibling_rects: list[Rect] = []

    def find_taskbar(self):
        self.calls.append(("find_taskbar",))
        return 10

    def find_notification_area(self, taskbar):
        self.calls.append(("find_notification_area", taskbar))
        return 20

    def get_rect(self, handle):
        self.calls.append(("get_rect", handle))
        if handle == 10:
            return Rect(0, 1032, 1920, 1080)
        return self.notification_rect

    def create_window(self, *, parent, style, ex_style, text):
        self.calls.append(("create_window", parent, style, ex_style, text))
        return 30

    def attach_to_taskbar(self, handle, taskbar):
        self.calls.append(("attach_to_taskbar", handle, taskbar))
        return self.attach_succeeds

    def list_sibling_rects(self, taskbar, exclude_handle):
        self.calls.append(("list_sibling_rects", taskbar, exclude_handle))
        return list(self.sibling_rects)

    def set_colorkey_transparency(self, handle):
        self.calls.append(("set_colorkey_transparency", handle))

    def move_window(self, handle, rect, *, topmost):
        self.calls.append(("move_window", handle, rect, topmost))

    def set_text(self, handle, text):
        self.calls.append(("set_text", handle, text))

    def set_visible(self, handle, visible):
        self.calls.append(("set_visible", handle, visible))

    def pump_messages(self, stop_requested, duration_seconds):
        self.calls.append(("pump_messages", duration_seconds))
        self.pump_rounds -= 1
        if self.pump_rounds <= 0:
            stop_requested.set()

    def close_window(self, handle):
        self.calls.append(("close_window", handle))


def test_native_popup_is_created_non_activating_with_hello_world():
    native = _FakeNativeWindow()
    companion = TaskbarCompanion(native=native)

    companion._run()

    create_call = next(call for call in native.calls if call[0] == "create_window")
    _, parent, style, ex_style, text = create_call
    assert parent == 0
    assert style & (_WS_POPUP | _WS_VISIBLE) == (_WS_POPUP | _WS_VISIBLE)
    assert ex_style & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE) == (
        _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
    )
    assert text == HELLO_WORLD_TEXT


def test_usage_text_supplied_before_start_is_rendered_initially():
    native = _FakeNativeWindow()
    companion = TaskbarCompanion(native=native)
    companion.update("Claude: 80% (3 hours)")

    companion._run()

    create_call = next(call for call in native.calls if call[0] == "create_window")
    assert create_call[-1] == "Claude: 80% (3 hours)"


def test_visibility_can_be_disabled_before_start():
    native = _FakeNativeWindow()
    companion = TaskbarCompanion(native=native, initial_visible=False)

    companion._run()

    create_call = next(call for call in native.calls if call[0] == "create_window")
    assert create_call[2] & _WS_VISIBLE == 0


def test_usage_text_updates_while_native_window_is_running():
    native = _FakeNativeWindow()
    companion = TaskbarCompanion(native=native)
    original_pump = native.pump_messages

    def update_during_first_pump(stop_requested, duration_seconds):
        companion.update("Claude: 80% (3 hours)")
        original_pump(stop_requested, duration_seconds)

    native.pump_messages = update_during_first_pump

    companion._run()

    assert ("set_text", 30, "Claude: 80% (3 hours)") in native.calls


def test_visibility_updates_while_native_window_is_running():
    native = _FakeNativeWindow()
    companion = TaskbarCompanion(native=native)
    original_pump = native.pump_messages

    def hide_during_first_pump(stop_requested, duration_seconds):
        companion.set_visible(False)
        original_pump(stop_requested, duration_seconds)

    native.pump_messages = hide_during_first_pump

    companion._run()

    assert ("set_visible", 30, False) in native.calls


def test_companion_embeds_into_the_taskbar_like_trafficmonitor():
    # TrafficMonitor's approach: SetParent into Shell_TrayWnd, color-key
    # transparency, and parent-relative placement before TrayNotifyWnd.
    native = _FakeNativeWindow(pump_rounds=2)

    TaskbarCompanion(native=native)._run()

    assert ("attach_to_taskbar", 30, 10) in native.calls
    assert ("set_colorkey_transparency", 30) in native.calls
    move_calls = [call for call in native.calls if call[0] == "move_window"]
    assert move_calls[0] == (
        "move_window",
        30,
        Rect(left=1362, top=0, right=1542, bottom=48),
        False,
    )


def test_embedded_companion_yields_space_to_other_taskbar_plugins():
    native = _FakeNativeWindow(pump_rounds=2)
    native.sibling_rects = [Rect(1440, 1032, 1542, 1080)]

    TaskbarCompanion(native=native)._run()

    move_calls = [call for call in native.calls if call[0] == "move_window"]
    assert move_calls[0][2] == Rect(left=1260, top=0, right=1440, bottom=48)


def test_embedded_companion_does_not_reposition_while_geometry_is_stable():
    native = _FakeNativeWindow(pump_rounds=3)

    TaskbarCompanion(native=native)._run()

    move_calls = [call for call in native.calls if call[0] == "move_window"]
    assert len(move_calls) == 1


def test_embedded_companion_follows_notification_area_changes():
    native = _FakeNativeWindow(pump_rounds=2)
    original_pump = native.pump_messages

    def pump_and_grow_notification_area(stop_requested, duration_seconds):
        original_pump(stop_requested, duration_seconds)
        native.notification_rect = Rect(1500, 1032, 1920, 1080)

    native.pump_messages = pump_and_grow_notification_area

    TaskbarCompanion(native=native)._run()

    move_calls = [call for call in native.calls if call[0] == "move_window"]
    assert move_calls[0][2] == Rect(left=1362, top=0, right=1542, bottom=48)
    assert move_calls[-1] == (
        "move_window",
        30,
        Rect(left=1320, top=0, right=1500, bottom=48),
        False,
    )


def test_native_window_is_destroyed_when_its_message_loop_finishes():
    native = _FakeNativeWindow()

    TaskbarCompanion(native=native)._run()

    assert native.calls[-1] == ("close_window", 30)


def test_companion_falls_back_to_topmost_screen_popup_when_parenting_fails():
    # Windows 11 periodically raises the taskbar above every other topmost
    # window, so the fallback popup must keep re-asserting its own position.
    native = _FakeNativeWindow(pump_rounds=3, attach_succeeds=False)

    TaskbarCompanion(native=native)._run()

    assert not any(call[0] == "set_colorkey_transparency" for call in native.calls)
    move_calls = [call for call in native.calls if call[0] == "move_window"]
    assert len(move_calls) == 1 + 3
    assert all(call[3] is True for call in move_calls)
    assert move_calls[0][2] == Rect(left=1362, top=1032, right=1542, bottom=1080)


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
