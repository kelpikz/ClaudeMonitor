"""Exercise the taskbar label against the real Windows API.

Every other Win32 test drives fake DLLs, and a fake cannot notice a mistyped
``ctypes`` signature: a wrong argument type only fails when Windows itself is
called. These tests therefore touch the real ``user32``, creating and
immediately destroying a hidden window, so a broken binding is caught here
instead of silently disabling the feature on the user's machine.

Nothing here is ever made visible and no taskbar window is modified.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the taskbar label is a Windows-only feature"
)

from claudemonitor.models import LabelSegment, Rect
from claudemonitor.taskbar_companion import companion_slot
from claudemonitor.win32_bindings import (
    IDC_ARROW,
    NONCLIENTMETRICSW,
    SPI_GETNONCLIENTMETRICS,
    USER_DEFAULT_SCREEN_DPI,
    USER32_SIGNATURES,
    apply_signatures,
)
from claudemonitor.win32_taskbar_window import (
    Win32TaskbarWindow,
    enable_per_monitor_dpi_awareness,
    process_dpi_awareness,
)


@pytest.fixture(scope="module", autouse=True)
def per_monitor_dpi_awareness() -> None:
    """Run every live test in the DPI mode the real application starts in.

    Without this the whole module measures the taskbar through Windows' DPI
    virtualization layer, which is the identity transform at 100% scaling and
    therefore hides placement bugs on exactly the scaled laptop displays where
    they occur.
    """
    enable_per_monitor_dpi_awareness()


@pytest.fixture
def native() -> Win32TaskbarWindow:
    return Win32TaskbarWindow()


class TestSignaturesAcceptTheValuesWePass:
    """A declared argument type that rejects our own constant is a broken binding."""

    def test_the_arrow_cursor_loads_through_the_declared_signature(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        apply_signatures(user32, USER32_SIGNATURES)

        assert user32.LoadCursorW(None, IDC_ARROW)


class TestWindowClass:
    def test_the_label_class_registers(self, native):
        native._register_class()

    def test_registering_twice_is_not_an_error(self, native):
        native._register_class()
        native._register_class()


class TestRealWindowLifecycle:
    def test_a_hidden_label_can_be_created_updated_moved_and_destroyed(self, native):
        handle = native.create_window(text="Claude: 80% (3 hours)")
        try:
            assert handle
            native.enable_per_pixel_alpha(handle)
            native.set_segments(
                handle, [LabelSegment("claude", "79% (2 hours)")]
            )
            native.move_window(
                handle, Rect(left=0, top=0, right=180, bottom=40), topmost=True
            )
            assert native.get_rect(handle).width == 180
        finally:
            native.close_window(handle)

    def test_a_two_provider_label_survives_the_real_api(self, native):
        # Two marks and two strings are measured by the real GDI, which is where
        # a mistyped signature actually shows up.
        handle = native.create_window(text="loading...")
        try:
            native.set_segments(
                handle,
                [
                    LabelSegment("claude", "80% (3h 0m)"),
                    LabelSegment("codex", "64% (2h 0m)"),
                ],
            )
            width = native.content_width_for(
                [
                    LabelSegment("claude", "80% (3h 0m)"),
                    LabelSegment("codex", "64% (2h 14m)"),
                ]
            )
            # Stacked rows share one width, so the longer string sets it and
            # the shorter one adds nothing.
            assert width == native.content_width_for(
                [LabelSegment("codex", "64% (2h 14m)")]
            )
            assert width > native.content_width_for(
                [LabelSegment("claude", "80% (3h 0m)")]
            )
        finally:
            native.close_window(handle)

    def test_a_real_paint_is_accepted_by_windows(self, native):
        """The whole render, against the real GDI, on a window nobody can see.

        CreateDIBSection hands back a pointer through an argument, and
        UpdateLayeredWindow takes nine of them including two structures. A fake
        DLL accepts any of that happily; only Windows objects to a wrong one,
        and the label would then be silently blank on the user's taskbar.
        """
        handle = native.create_window(text="loading...")
        try:
            native.enable_per_pixel_alpha(handle)
            native.move_window(
                handle, Rect(left=-3000, top=-3000, right=-2860, bottom=-2940),
                topmost=True,
            )
            native.set_segments(
                handle,
                [
                    LabelSegment("claude", "80% (3h 0m)"),
                    LabelSegment("codex", "64% (2h 14m)"),
                ],
            )

            native._render_label(handle)
        finally:
            native.close_window(handle)

    def test_a_paint_before_the_label_is_sized_is_skipped(self, native):
        # A window is born 1x1 and only sized once the taskbar is measured.
        # Windows refuses a bitmap for an empty rectangle, so the paint has to
        # notice rather than log a failure every second until the first move.
        handle = native.create_window(text="loading...")
        try:
            native.enable_per_pixel_alpha(handle)
            native.move_window(
                handle, Rect(left=-3000, top=-3000, right=-3000, bottom=-3000),
                topmost=True,
            )

            native._render_label(handle)
        finally:
            native.close_window(handle)

    def test_closing_a_destroyed_label_is_safe(self, native):
        handle = native.create_window(text="Claude: loading...")
        native.close_window(handle)
        native.close_window(handle)


class TestLiveDpiAwareness:
    """Coordinates only survive the trip into Explorer if both sides agree on DPI."""

    def test_the_process_matches_the_taskbars_per_monitor_awareness(self, native):
        """A mismatch silently mis-sizes the label on any scaled display.

        Explorer's taskbar is per-monitor aware. While ClaudeMonitor was DPI
        unaware, a child placed inside it came back scaled by 1/display-scale —
        180x48 became 144x38 at 125%. At 100% the factor is 1.0, so asserting
        the awareness itself is the only check that fails on every machine
        rather than only on a scaled one.
        """
        taskbar = native.find_taskbar()

        assert process_dpi_awareness() == native.window_dpi_awareness(taskbar)

    def test_windows_accepts_our_metrics_struct_for_a_per_dpi_font_query(self, native):
        """A struct Windows rejects would silently downgrade the label's font.

        SystemParametersInfoForDpi validates the size of the structure it is
        handed and answers a plain zero when it disagrees — no exception, no
        error to notice. The legacy SystemParametersInfoW still accepts the
        pre-Vista layout for compatibility, so only calling the per-DPI variant
        for real proves NONCLIENTMETRICSW matches this Windows build.
        """
        metrics = NONCLIENTMETRICSW()
        metrics.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)

        accepted = native._user32.SystemParametersInfoForDpi(
            SPI_GETNONCLIENTMETRICS,
            ctypes.sizeof(NONCLIENTMETRICSW),
            ctypes.byref(metrics),
            0,
            USER_DEFAULT_SCREEN_DPI,
        )

        assert accepted
        assert metrics.lfMessageFont.lfFaceName


class TestLivePlacement:
    """Attach a real label to the real taskbar, but never reveal it."""

    def test_a_label_lands_inside_the_taskbar_left_of_the_clock(self, native):
        taskbar = native.find_taskbar()
        notification = native.find_notification_area(taskbar)
        taskbar_rect = native.get_rect(taskbar)
        notification_rect = native.get_rect(notification)

        handle = native.create_window(text="Claude: 80% (3 hours)")
        try:
            assert native.attach_to_taskbar(handle, taskbar)

            slot = companion_slot(
                taskbar_rect,
                notification_rect,
                native.list_sibling_rects(taskbar, handle),
                width=180,
            )
            native.move_window(handle, slot, topmost=False)

            # A child window is placed in taskbar-relative coordinates, so the
            # screen rectangle Windows reports back proves the conversion held.
            placed = native.get_rect(handle)
            assert placed.width == slot.width
            assert placed.left >= taskbar_rect.left
            assert placed.right <= notification_rect.left
        finally:
            native.close_window(handle)


class TestDiscovery:
    """Read-only queries against the live shell; nothing is modified."""

    def test_the_taskbar_and_its_notification_area_are_found(self, native):
        taskbar = native.find_taskbar()
        notification = native.find_notification_area(taskbar)

        assert native.get_rect(taskbar).width > 0
        assert native.get_rect(notification).width > 0

    def test_taskbar_siblings_are_measurable(self, native):
        taskbar = native.find_taskbar()

        assert all(rect.width > 0 for rect in native.list_sibling_rects(taskbar, 0))
