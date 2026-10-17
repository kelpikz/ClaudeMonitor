"""Tests for the fonts the windows are drawn in.

The DLLs are fakes: the system metrics query fills in a font description the
test chooses, and the font Windows is asked for is read back from the call.
"""

from __future__ import annotations

import ctypes

from claudemonitor.win32_bindings import FF_MODERN, FIXED_PITCH, LOGFONTW
from claudemonitor.win32_text import MONOSPACE_FACE, create_monospace_font


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


def _system_font(height: int = -12):
    """A user32 whose metrics query reports a Segoe UI message font."""

    def fill(_action, _size, pointer, _flags, *_dpi):
        pointer._obj.lfMessageFont.lfHeight = height
        pointer._obj.lfMessageFont.lfFaceName = "Segoe UI"
        return 1

    return _FakeDll(SystemParametersInfoForDpi=fill)


class _FontGdi(_FakeDll):
    """A gdi32 that keeps a copy of every font description it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[LOGFONTW] = []

    def CreateFontIndirectW(self, pointer):
        self.calls.append(("CreateFontIndirectW",))
        requested = LOGFONTW()
        ctypes.memmove(ctypes.byref(requested), pointer, ctypes.sizeof(LOGFONTW))
        self.requested.append(requested)
        return 4242


class TestTheMonospaceFont:
    def test_it_asks_for_consolas(self):
        gdi32 = _FontGdi()

        create_monospace_font(_system_font(), gdi32, 96)

        assert MONOSPACE_FACE == "Consolas"
        assert gdi32.requested[0].lfFaceName == "Consolas"

    def test_it_asks_for_any_fixed_pitch_font_if_consolas_is_missing(self):
        gdi32 = _FontGdi()

        create_monospace_font(_system_font(), gdi32, 96)

        assert gdi32.requested[0].lfPitchAndFamily == FIXED_PITCH | FF_MODERN

    def test_it_is_as_tall_as_the_system_ui_font_at_this_dpi(self):
        gdi32 = _FontGdi()

        create_monospace_font(_system_font(height=-18), gdi32, 144)

        assert gdi32.requested[0].lfHeight == -18

    def test_it_is_a_font_of_its_own_that_the_caller_releases(self):
        font, is_stock = create_monospace_font(_system_font(), _FontGdi(), 96)

        assert (font, is_stock) == (4242, False)

    def test_without_system_metrics_it_is_nine_points_at_this_dpi(self):
        gdi32 = _FontGdi()
        user32 = _FakeDll(SystemParametersInfoForDpi=0, SystemParametersInfoW=0)

        create_monospace_font(user32, gdi32, 144)

        assert gdi32.requested[0].lfHeight == -18
        assert gdi32.requested[0].lfFaceName == "Consolas"
