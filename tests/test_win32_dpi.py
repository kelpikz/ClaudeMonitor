"""Cover the label's behaviour on displays that are not at 100% scaling.

Explorer's taskbar is per-monitor DPI aware. A DPI-unaware process that parents
a child into it has that child's coordinates virtualized, so a slot requested as
180x48 arrives as 144x38 on a 125% display — the label is drawn at 80% of the
size it asked for and lands in the wrong place. These tests pin the two halves
of the fix: the process declares the same awareness as the taskbar, and every
96-DPI design constant is scaled to the display the label currently sits on.
"""

from __future__ import annotations

import pytest

from claudemonitor import win32_taskbar_window
from claudemonitor.win32_bindings import USER_DEFAULT_SCREEN_DPI
from claudemonitor.win32_taskbar_window import (
    _ICON_CONTENT_RIGHT_PADDING,
    _ICON_LEFT_INSET,
    _ICON_SIZE,
    _ICON_TEXT_GAP,
    enable_per_monitor_dpi_awareness,
    scale_for_dpi,
)

from tests.test_win32_taskbar_window import _FakeDll, _FakeUser32, _window


_DPI_100_PERCENT = 96
_DPI_125_PERCENT = 120
_DPI_150_PERCENT = 144

# Width GetTextExtentPoint32W is made to report, so a measured width can be
# told apart from the insets surrounding it.
_MEASURED_TEXT_WIDTH = 100


class _FakeUser32AtDpi(_FakeUser32):
    """A fake taskbar whose display scaling the test can change at will."""

    def __init__(self, dpi: int = _DPI_100_PERCENT) -> None:
        super().__init__()
        self.dpi = dpi
        self.metrics_requests: list[int] = []

    def GetDpiForWindow(self, handle):
        self.calls.append(("GetDpiForWindow", handle))
        return self.dpi

    def SystemParametersInfoForDpi(self, action, size, metrics_pointer, flags, dpi):
        self.calls.append(("SystemParametersInfoForDpi", action, size, flags, dpi))
        self.metrics_requests.append(dpi)
        return self.results.get("SystemParametersInfoForDpi", 1)


class _FakeGdi32Measuring(_FakeDll):
    """Report a fixed text width so inset scaling is the only variable."""

    def __init__(self, text_width: int = _MEASURED_TEXT_WIDTH) -> None:
        super().__init__()
        self.text_width = text_width
        self.created_fonts: list[int] = []
        self.deleted_objects: list[int] = []
        self._next_font = 500

    def GetTextExtentPoint32W(self, device_context, text, length, size_pointer):
        self.calls.append(("GetTextExtentPoint32W", text, length))
        size_pointer._obj.cx = self.text_width
        return 1

    def CreateFontIndirectW(self, logfont_pointer):
        self.calls.append(("CreateFontIndirectW",))
        self._next_font += 1
        self.created_fonts.append(self._next_font)
        return self._next_font

    def DeleteObject(self, handle):
        self.calls.append(("DeleteObject", handle))
        self.deleted_objects.append(handle)
        return 1


def _label_at_dpi(dpi: int) -> tuple[win32_taskbar_window.Win32TaskbarWindow, _FakeUser32AtDpi, _FakeGdi32Measuring]:
    """Build an adapter that believes its label sits on a display at ``dpi``."""
    user32 = _FakeUser32AtDpi(dpi)
    gdi32 = _FakeGdi32Measuring()
    native = _window(user32=user32, gdi32=gdi32)
    native._label_handle = 4242
    return native, user32, gdi32


class TestScaleForDpi:
    """Design constants are written for 96 DPI and scaled to the real display."""

    def test_an_unscaled_display_leaves_a_constant_untouched(self):
        assert scale_for_dpi(_ICON_SIZE, USER_DEFAULT_SCREEN_DPI) == _ICON_SIZE

    def test_a_125_percent_display_scales_a_constant_up_by_a_quarter(self):
        assert scale_for_dpi(16, _DPI_125_PERCENT) == 20

    def test_a_150_percent_display_scales_a_constant_up_by_a_half(self):
        assert scale_for_dpi(16, _DPI_150_PERCENT) == 24

    def test_a_fractional_result_is_rounded_to_a_whole_pixel(self):
        # 6px at 125% is 7.5px; a fractional pixel cannot be drawn.
        assert scale_for_dpi(6, _DPI_125_PERCENT) == 8

    def test_a_nonsensical_dpi_falls_back_to_the_unscaled_constant(self):
        # GetDpiForWindow returns 0 for a handle Windows no longer recognizes.
        assert scale_for_dpi(_ICON_SIZE, 0) == _ICON_SIZE


class TestProcessDpiAwareness:
    """The process must declare the same awareness as the taskbar it joins."""

    def test_the_modern_per_monitor_context_is_requested_first(self):
        calls: list[int] = []
        user32 = type(
            "_Exports",
            (),
            {"SetProcessDpiAwarenessContext": staticmethod(
                lambda context: calls.append(context) or 1
            )},
        )()

        assert enable_per_monitor_dpi_awareness(user32=user32) is True
        assert calls == [win32_taskbar_window.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2]

    def test_an_older_windows_without_the_export_falls_back_to_system_awareness(self):
        # Windows 8.1 and earlier have only the process-wide SetProcessDPIAware.
        fallback_calls: list[bool] = []
        user32 = type(
            "_OldExports",
            (),
            {"SetProcessDPIAware": staticmethod(
                lambda: fallback_calls.append(True) or 1
            )},
        )()

        assert enable_per_monitor_dpi_awareness(user32=user32) is False
        assert fallback_calls == [True]

    def test_a_rejected_request_falls_back_instead_of_raising(self):
        # Windows refuses a second call once awareness is already set; the app
        # must keep starting rather than die in its first statement.
        fallback_calls: list[bool] = []
        user32 = type(
            "_Rejecting",
            (),
            {
                "SetProcessDpiAwarenessContext": staticmethod(lambda context: 0),
                "SetProcessDPIAware": staticmethod(
                    lambda: fallback_calls.append(True) or 1
                ),
            },
        )()

        assert enable_per_monitor_dpi_awareness(user32=user32) is False
        assert fallback_calls == [True]

    def test_a_process_with_no_dpi_exports_at_all_is_survivable(self):
        assert enable_per_monitor_dpi_awareness(user32=type("_None", (), {})()) is False


class TestFontFollowsDisplayScaling:
    """A cached font is why moving to a second monitor kept the old scaling."""

    def test_the_ui_font_is_requested_at_the_labels_own_dpi(self):
        native, user32, _gdi32 = _label_at_dpi(_DPI_125_PERCENT)

        native.message_font(_DPI_125_PERCENT)

        assert user32.metrics_requests == [_DPI_125_PERCENT]

    def test_an_unchanged_dpi_reuses_the_font_it_already_built(self):
        native, _user32, gdi32 = _label_at_dpi(_DPI_125_PERCENT)

        first = native.message_font(_DPI_125_PERCENT)
        second = native.message_font(_DPI_125_PERCENT)

        assert first == second
        assert len(gdi32.created_fonts) == 1

    def test_moving_to_a_differently_scaled_monitor_rebuilds_the_font(self):
        native, _user32, gdi32 = _label_at_dpi(_DPI_125_PERCENT)

        laptop_font = native.message_font(_DPI_125_PERCENT)
        external_font = native.message_font(_DPI_100_PERCENT)

        assert laptop_font != external_font
        assert len(gdi32.created_fonts) == 2

    def test_the_replaced_font_is_deleted_so_rescaling_cannot_leak_handles(self):
        native, _user32, gdi32 = _label_at_dpi(_DPI_125_PERCENT)

        laptop_font = native.message_font(_DPI_125_PERCENT)
        native.message_font(_DPI_100_PERCENT)

        assert gdi32.deleted_objects == [laptop_font]

    def test_a_windows_build_without_the_per_dpi_metrics_call_still_gets_a_font(self):
        # SystemParametersInfoForDpi arrived in Windows 10 1607.
        user32 = _FakeUser32()
        gdi32 = _FakeGdi32Measuring()
        native = _window(user32=user32, gdi32=gdi32)
        native._label_handle = 4242

        assert native.message_font(_DPI_125_PERCENT) is not None

    def test_a_stock_fallback_font_is_never_deleted_on_the_next_rescale(self):
        """Stock objects belong to Windows; deleting one is not ours to do."""
        user32 = _FakeUser32AtDpi(_DPI_125_PERCENT)
        user32.results["SystemParametersInfoForDpi"] = 0
        user32.results["SystemParametersInfoW"] = 0
        gdi32 = _FakeGdi32Measuring()
        gdi32.results["GetStockObject"] = 66
        native = _window(user32=user32, gdi32=gdi32)
        native._label_handle = 4242

        assert native.message_font(_DPI_125_PERCENT) == 66
        native.message_font(_DPI_100_PERCENT)

        assert 66 not in gdi32.deleted_objects

    def test_a_rejected_per_dpi_query_still_reads_the_legacy_metrics(self):
        """Falling straight through to the stock font would change the typeface.

        The per-DPI query rejects anything it dislikes about the request rather
        than raising, so refusing it must not cost the label Segoe UI — the
        legacy query answers for the primary display, which is wrong only when
        a second monitor scales differently.
        """
        user32 = _FakeUser32AtDpi(_DPI_125_PERCENT)
        user32.results["SystemParametersInfoForDpi"] = 0
        gdi32 = _FakeGdi32Measuring()
        gdi32.results["GetStockObject"] = 66
        native = _window(user32=user32, gdi32=gdi32)
        native._label_handle = 4242

        font = native.message_font(_DPI_125_PERCENT)

        assert user32.was_called("SystemParametersInfoW")
        assert font != 66
        assert font in gdi32.created_fonts


class TestContentWidthFollowsDisplayScaling:
    """The measured slot has to grow with the display, or the text is clipped."""

    def test_the_icon_and_padding_are_scaled_to_the_display(self):
        native, _user32, _gdi32 = _label_at_dpi(_DPI_125_PERCENT)

        width = native.content_width_for("Claude: 80% (3 hours)")

        expected_insets = sum(
            scale_for_dpi(constant, _DPI_125_PERCENT)
            for constant in (
                _ICON_LEFT_INSET,
                _ICON_SIZE,
                _ICON_TEXT_GAP,
                _ICON_CONTENT_RIGHT_PADDING,
            )
        )
        assert width == expected_insets + _MEASURED_TEXT_WIDTH

    def test_a_scaled_display_reserves_more_room_than_an_unscaled_one(self):
        scaled, _u1, _g1 = _label_at_dpi(_DPI_150_PERCENT)
        unscaled, _u2, _g2 = _label_at_dpi(_DPI_100_PERCENT)

        text = "Claude: 80% (3 hours)"
        assert scaled.content_width_for(text) > unscaled.content_width_for(text)

    def test_a_label_that_does_not_exist_yet_measures_at_the_unscaled_default(self):
        user32 = _FakeUser32AtDpi(_DPI_125_PERCENT)
        native = _window(user32=user32, gdi32=_FakeGdi32Measuring())

        # No window has been created, so there is no monitor to ask about.
        assert native.content_width_for("Claude") == (
            _ICON_LEFT_INSET
            + _ICON_SIZE
            + _ICON_TEXT_GAP
            + _ICON_CONTENT_RIGHT_PADDING
            + _MEASURED_TEXT_WIDTH
        )


class TestScalingEndToEndThroughTheAdapter:
    """Drive the whole adapter from 'Windows reports a DPI' to 'the label is sized'."""

    @pytest.mark.parametrize(
        "dpi,scale_name",
        [(_DPI_100_PERCENT, "100%"), (_DPI_125_PERCENT, "125%"), (_DPI_150_PERCENT, "150%")],
    )
    def test_a_label_created_on_a_scaled_display_measures_and_paints_at_that_scale(
        self, dpi, scale_name
    ):
        native, user32, gdi32 = _label_at_dpi(dpi)

        width = native.content_width_for("Claude: 80% (3 hours)")

        # The font Windows was asked for and the width the slot reserves must
        # agree on one scale factor, or the text overflows its own slot.
        expected_insets = sum(
            scale_for_dpi(constant, dpi)
            for constant in (
                _ICON_LEFT_INSET,
                _ICON_SIZE,
                _ICON_TEXT_GAP,
                _ICON_CONTENT_RIGHT_PADDING,
            )
        )
        assert user32.metrics_requests == [dpi], scale_name
        assert width == expected_insets + _MEASURED_TEXT_WIDTH, scale_name

    def test_the_second_monitor_case_rescales_without_recreating_the_window(self):
        """The reported bug: the laptop's 125% survived onto a 100% display."""
        native, user32, _gdi32 = _label_at_dpi(_DPI_125_PERCENT)
        text = "Claude: 80% (3 hours)"

        on_laptop = native.content_width_for(text)

        # The taskbar moves to the external display; only the DPI changes.
        user32.dpi = _DPI_100_PERCENT
        on_external = native.content_width_for(text)

        assert on_laptop > on_external
        assert user32.metrics_requests == [_DPI_125_PERCENT, _DPI_100_PERCENT]
