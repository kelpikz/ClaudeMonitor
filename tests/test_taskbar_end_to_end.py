"""End-to-end coverage for the taskbar label.

Every test here starts at the Anthropic API response and finishes at the exact
string the native window is asked to paint, driven through the real poll cycle
so no layer can drift from another.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claudemonitor import fetcher, main
from claudemonitor.config import Config
from claudemonitor.models import Rect
from claudemonitor.poll_cycle import PollCycle
from claudemonitor.taskbar_companion import TaskbarCompanion


NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
TASKBAR = Rect(left=0, top=1032, right=1920, bottom=1080)
NOTIFICATION = Rect(left=1542, top=1032, right=1920, bottom=1080)


class _RecordingNativeWindow:
    """Capture what Windows would paint and show on hover, without touching Win32."""

    def __init__(self) -> None:
        self.painted: list[str] = []
        self.tooltips: list[str] = []

    def find_taskbar(self) -> int:
        return 10

    def find_notification_area(self, taskbar: int) -> int:
        return 20

    def get_rect(self, handle: int) -> Rect:
        return TASKBAR if handle == 10 else NOTIFICATION

    def content_width_for(self, text: str) -> int:
        return 180

    def create_window(self, *, text: str) -> int:
        self.painted.append(text)
        return 30

    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool:
        return True

    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]:
        return []

    def set_colorkey_transparency(self, handle: int) -> None:
        pass

    def refresh_theme(self, handle: int) -> None:
        pass

    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None:
        pass

    def set_text(self, handle: int, text: str) -> None:
        self.painted.append(text)

    def set_tooltip(self, handle: int, tooltip: str) -> None:
        self.tooltips.append(tooltip)

    def set_visible(self, handle: int, visible: bool) -> None:
        pass

    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None:
        stop_requested.set()

    def close_window(self, handle: int) -> None:
        pass


class _StubPresenter:
    """Absorb the tray half of the display so only the taskbar is asserted."""

    def apply(self, icon, state) -> None:
        pass

    def notify(self, icon, title, message) -> None:
        pass


def _respond_with(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    """Make fetcher.fetch see one canned Anthropic API response."""
    monkeypatch.setattr(
        fetcher,
        "_read_credentials",
        lambda: fetcher.Credentials(access_token="token", expires_at=None),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)


def _usage_response(five_hour: dict | None, seven_day: dict | None = None) -> httpx.Response:
    body: dict[str, object] = {}
    if five_hour is not None:
        body["five_hour"] = five_hour
    if seven_day is not None:
        body["seven_day"] = seven_day
    return httpx.Response(200, json=body, request=httpx.Request("GET", "https://example.test"))


def _cycle_painting_into(native: _RecordingNativeWindow, presenter=None) -> tuple:
    """Build the real poll cycle wired to a recording native window."""
    companion = TaskbarCompanion(native=native)
    cycle = PollCycle(
        Config(),
        display=main.TrayAndTaskbarDisplay(
            presenter or _StubPresenter(), object(), companion
        ),
        fetch=fetcher.fetch,
        # Freeze the clock so reset countdowns are deterministic.
        now=lambda: NOW,
    )
    return cycle, companion


def _painted_label() -> str:
    """Run one full fetch -> process -> display -> native paint cycle."""
    native = _RecordingNativeWindow()
    cycle, companion = _cycle_painting_into(native)

    state = cycle.run_once()

    companion._run()
    # Every response case below proves the tray's processed detail reaches the
    # native taskbar hover UI unchanged.
    assert native.tooltips[-1] == state.tooltip
    return native.painted[-1]


class TestHappyPath:
    def test_live_usage_reaches_the_native_label(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()},
                {"utilization": 36.0, "resets_at": (NOW + timedelta(days=4)).isoformat()},
            ),
        )

        assert _painted_label() == "80% (3h 0m)"

    def test_an_unstarted_session_is_labelled_honestly(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response({"utilization": 0.0, "resets_at": None}),
        )

        assert _painted_label() == "100% (not started)"

    def test_a_nearly_exhausted_window_reaches_the_native_label(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response(
                {"utilization": 99.5, "resets_at": (NOW + timedelta(minutes=12)).isoformat()}
            ),
        )

        assert _painted_label() == "0% (12m)"


class TestErrorPaths:
    """Each failure mode must reach the user as its own message, not one blank
    placeholder that hides why usage stopped updating."""

    def test_expired_token(self, monkeypatch):
        _respond_with(
            monkeypatch,
            httpx.Response(401, request=httpx.Request("GET", "https://example.test")),
        )

        assert _painted_label() == "token expired"

    def test_rate_limited_without_previous_data(self, monkeypatch):
        _respond_with(
            monkeypatch,
            httpx.Response(429, request=httpx.Request("GET", "https://example.test")),
        )

        assert _painted_label() == "rate limited"

    def test_network_failure(self, monkeypatch):
        monkeypatch.setattr(
            fetcher,
            "_read_credentials",
            lambda: fetcher.Credentials(access_token="token", expires_at=None),
        )

        def refuse(*args, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "get", refuse)

        assert _painted_label() == "offline"

    def test_missing_credentials(self, monkeypatch):
        def missing():
            raise FileNotFoundError

        monkeypatch.setattr(fetcher, "_read_credentials", missing)

        assert _painted_label() == "not logged in"

    def test_unexpected_response_shape(self, monkeypatch):
        _respond_with(monkeypatch, _usage_response({"nonsense": True}))

        assert _painted_label() == "bad response"

    def test_response_without_usage_windows(self, monkeypatch):
        _respond_with(monkeypatch, _usage_response(None))

        assert _painted_label() == "no data"


class TestResilience:
    """A tray surface that raises must not end the loop feeding the taskbar."""

    class _BrokenPresenter:
        def apply(self, icon, state):
            raise ValueError("string too long")

        def notify(self, icon, title, message):
            pass

    def test_a_failing_tray_does_not_stop_the_next_cycle(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()}
            ),
        )
        native = _RecordingNativeWindow()
        cycle, _companion = _cycle_painting_into(native, self._BrokenPresenter())

        cycle.run_once()
        second = cycle.run_once()

        assert second.taskbar_text == "80% (3h 0m)"
