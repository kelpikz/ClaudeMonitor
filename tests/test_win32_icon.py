"""Tests for making a window's icons from PNG bytes.

user32 is replaced by a recording fake, so the sizes asked for, the icons set,
and the icons freed can be checked without a window.
"""

from __future__ import annotations

from claudemonitor.win32_bindings import (
    ICON_BIG,
    ICON_RESOURCE_VERSION,
    ICON_SMALL,
    SM_CXICON,
    SM_CXSMICON,
    WM_SETICON,
)
from claudemonitor.win32_icon import destroy_icons, icon_from_png, set_window_icons

_WINDOW = 4242


class _FakeUser32:
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

    def named(self, name: str) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] == name]


def _user32(*, icons=(501, 502), sizes=None) -> _FakeUser32:
    """Build a user32 that hands out these icons and reports these icon sizes."""
    handed_out = iter(icons)
    metrics = sizes if sizes is not None else {SM_CXICON: 32, SM_CXSMICON: 16}
    return _FakeUser32(
        CreateIconFromResourceEx=lambda *args: next(handed_out),
        GetSystemMetrics=lambda metric: metrics.get(metric, 0),
    )


def _draw(size: int) -> bytes:
    """Stand in for the PNG drawing: bytes that name their size."""
    return f"png-{size}".encode()


class TestIconFromPng:
    def test_windows_is_given_the_png_bytes_at_the_size_asked_for(self):
        user32 = _user32(icons=(501,))

        icon = icon_from_png(user32, b"png-bytes", 24)

        call = user32.named("CreateIconFromResourceEx")[0]
        assert icon == 501
        assert bytes(call[1]) == b"png-bytes"
        assert call[2:] == (len(b"png-bytes"), True, ICON_RESOURCE_VERSION, 24, 24, 0)

    def test_a_refusal_is_zero(self):
        assert icon_from_png(_user32(icons=(0,)), b"png", 16) == 0


class TestSetWindowIcons:
    def test_the_big_and_the_small_icon_are_both_set(self):
        user32 = _user32()

        icons = set_window_icons(user32, _WINDOW, _draw)

        sent = [call[1:] for call in user32.named("SendMessageW")]
        assert sent == [(_WINDOW, WM_SETICON, ICON_BIG, 501), (_WINDOW, WM_SETICON, ICON_SMALL, 502)]
        assert icons == [501, 502]

    def test_each_icon_is_drawn_at_the_size_windows_reports(self):
        user32 = _user32(sizes={SM_CXICON: 48, SM_CXSMICON: 24})

        set_window_icons(user32, _WINDOW, _draw)

        made = [(bytes(call[1]), call[5]) for call in user32.named("CreateIconFromResourceEx")]
        assert made == [(b"png-48", 48), (b"png-24", 24)]

    def test_a_windows_that_will_not_say_gets_the_96_dpi_sizes(self):
        user32 = _user32(sizes={})

        set_window_icons(user32, _WINDOW, _draw)

        assert [call[5] for call in user32.named("CreateIconFromResourceEx")] == [32, 16]

    def test_an_icon_windows_cannot_make_is_skipped_and_logged(self, caplog):
        user32 = _user32(icons=(0, 502))

        icons = set_window_icons(user32, _WINDOW, _draw)

        assert icons == [502]
        assert [call[3] for call in user32.named("SendMessageW")] == [ICON_SMALL]
        assert "icon" in caplog.text


class TestDestroyIcons:
    def test_every_icon_is_freed(self):
        user32 = _FakeUser32()

        destroy_icons(user32, [501, 502])

        assert [call[1] for call in user32.named("DestroyIcon")] == [501, 502]
