from __future__ import annotations

import threading
from pathlib import Path

from claudemonitor import tray
from claudemonitor.models import DisplayState
from claudemonitor.tray import _MAX_TOOLTIP_LEN, _truncate_tooltip


class _FakeIcon:
    """Records whatever apply() assigns, standing in for a real pystray.Icon."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.menu = None


class TestTruncateTooltip:
    """The Windows tray tooltip (NOTIFYICONDATAW.szTip) is a fixed 128-WCHAR
    buffer; pystray raises ValueError above that, which would kill the poll
    thread. _truncate_tooltip guarantees we never exceed the limit."""

    def test_short_text_is_unchanged(self):
        text = "Claude usage\n5h: 70% left"
        assert _truncate_tooltip(text) == text

    def test_text_at_the_limit_is_unchanged(self):
        text = "x" * _MAX_TOOLTIP_LEN
        assert _truncate_tooltip(text) == text

    def test_over_limit_is_clipped_within_bounds(self):
        result = _truncate_tooltip("y" * 200)
        assert len(result) <= _MAX_TOOLTIP_LEN

    def test_over_limit_keeps_an_ellipsis_marker(self):
        result = _truncate_tooltip("y" * 200)
        assert result.endswith("…")

    def test_limit_stays_within_windows_128_cap(self):
        # The hard Windows cap is 128; our limit must sit at or below it.
        assert _MAX_TOOLTIP_LEN <= 128


class TestApplyNeverExceedsTooltipLimit:
    """End-to-end guard: even a pathologically long tooltip must not raise."""

    def test_apply_truncates_long_tooltip(self):
        tray.init(threading.Event(), Path("."))
        icon = _FakeIcon()
        state = DisplayState(
            icon_color="grey",
            tooltip="z" * 500,
            menu_status_label="Updated 1s ago",
        )
        tray.apply(icon, state)
        assert len(icon.title) <= _MAX_TOOLTIP_LEN
