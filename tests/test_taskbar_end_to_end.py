"""End-to-end coverage for the taskbar label.

Every test here starts at a provider's API response and finishes at the exact
string the native window is asked to paint, so no layer can drift from another.
The final class does it for both providers at once, which is what the label
actually shows.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claudemonitor import codex_fetcher, fetcher, main, processor
from claudemonitor.config import Config
from claudemonitor.models import CLAUDE, CODEX, Rect
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

    def content_width_for(self, segments) -> int:
        return 180

    def create_window(self, *, text: str) -> int:
        self.painted.append(text)
        return 30

    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool:
        return True

    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]:
        return []

    def enable_per_pixel_alpha(self, handle: int) -> None:
        pass

    def refresh_theme(self, handle: int) -> None:
        pass

    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None:
        pass

    def set_segments(self, handle: int, segments) -> None:
        self.painted.append("  ".join(segment.text for segment in segments))

    def set_tooltip(self, handle: int, tooltip: str) -> None:
        self.tooltips.append(tooltip)

    def set_visible(self, handle: int, visible: bool) -> None:
        pass

    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None:
        stop_requested.set()

    def close_window(self, handle: int) -> None:
        pass


class _StubIcon:
    """Absorb the tray half of the display update so only the taskbar is asserted."""

    def __init__(self) -> None:
        self.icon = None
        self.title = None
        self.menu = None


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


def _painted_label(monkeypatch: pytest.MonkeyPatch) -> str:
    """Run one full fetch -> process -> display -> native paint cycle."""
    native = _RecordingNativeWindow()
    companion = TaskbarCompanion(native=native)
    data = fetcher.fetch()
    # Freeze the clock so reset countdowns are deterministic.
    state = processor.process(data, now=NOW, config=Config())

    monkeypatch.setattr(main.tray, "apply", lambda icon, value: None)
    main._apply_display({"claude": _StubIcon()}, [state], companion)

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

        assert _painted_label(monkeypatch) == "80% (3h 0m)"

    def test_an_unstarted_session_is_labelled_honestly(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response({"utilization": 0.0, "resets_at": None}),
        )

        assert _painted_label(monkeypatch) == "100% (not started)"

    def test_a_nearly_exhausted_window_reaches_the_native_label(self, monkeypatch):
        _respond_with(
            monkeypatch,
            _usage_response(
                {"utilization": 99.5, "resets_at": (NOW + timedelta(minutes=12)).isoformat()}
            ),
        )

        assert _painted_label(monkeypatch) == "0% (12m)"


class TestErrorPaths:
    """Each failure mode must reach the user as its own message, not one blank
    placeholder that hides why usage stopped updating."""

    def test_expired_token(self, monkeypatch):
        _respond_with(
            monkeypatch,
            httpx.Response(401, request=httpx.Request("GET", "https://example.test")),
        )

        assert _painted_label(monkeypatch) == "token expired"

    def test_rate_limited_without_previous_data(self, monkeypatch):
        _respond_with(
            monkeypatch,
            httpx.Response(429, request=httpx.Request("GET", "https://example.test")),
        )

        assert _painted_label(monkeypatch) == "rate limited"

    def test_network_failure(self, monkeypatch):
        monkeypatch.setattr(
            fetcher,
            "_read_credentials",
            lambda: fetcher.Credentials(access_token="token", expires_at=None),
        )

        def refuse(*args, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "get", refuse)

        assert _painted_label(monkeypatch) == "offline"

    def test_missing_credentials(self, monkeypatch):
        def missing():
            raise FileNotFoundError

        monkeypatch.setattr(fetcher, "_read_credentials", missing)

        assert _painted_label(monkeypatch) == "not logged in"

    def test_unexpected_response_shape(self, monkeypatch):
        _respond_with(monkeypatch, _usage_response({"nonsense": True}))

        assert _painted_label(monkeypatch) == "bad response"

    def test_response_without_usage_windows(self, monkeypatch):
        _respond_with(monkeypatch, _usage_response(None))

        assert _painted_label(monkeypatch) == "no data"


class TestBothProvidersOnOneLabel:
    """The whole point of the feature: one label, both providers, in order."""

    def _codex_body(self, used_percent: float, reset_at: int | None) -> dict:
        return {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": used_percent,
                    "limit_window_seconds": 18000,
                    "reset_at": reset_at,
                }
            },
        }

    def _painted(self, monkeypatch, claude_response, codex_response) -> tuple[list, str]:
        """Run both fetchers through to the segments the native window paints."""
        monkeypatch.setattr(
            fetcher,
            "_read_credentials",
            lambda: fetcher.Credentials(access_token="token", expires_at=None),
        )
        monkeypatch.setattr(
            codex_fetcher,
            "_read_credentials",
            lambda: codex_fetcher.CodexCredentials("tok", "acct", None),
        )
        monkeypatch.setattr(httpx, "get", lambda *a, **k: claude_response)
        claude_data = fetcher.fetch()

        monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: codex_response)
        codex_data = codex_fetcher.fetch()

        states = [
            processor.process(claude_data, now=NOW, config=Config(), provider=CLAUDE),
            processor.process(codex_data, now=NOW, config=Config(), provider=CODEX),
        ]

        native = _RecordingNativeWindow()
        companion = TaskbarCompanion(native=native)
        monkeypatch.setattr(main.tray, "apply", lambda icon, value: None)
        main._apply_display(
            {"claude": _StubIcon(), "codex": _StubIcon()}, states, companion
        )
        companion._run()

        label = processor.taskbar_label(states)
        assert native.tooltips[-1] == label.tooltip
        return label.segments, native.painted[-1]

    def test_both_readings_reach_the_native_label(self, monkeypatch):
        segments, painted = self._painted(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()}
            ),
            httpx.Response(
                200,
                json=self._codex_body(
                    36.0, int((NOW + timedelta(hours=2)).timestamp())
                ),
                request=httpx.Request("GET", "https://example.test"),
            ),
        )

        assert [(s.provider_key, s.text) for s in segments] == [
            ("claude", "80% (3h 0m)"),
            ("codex", "64% (2h 0m)"),
        ]
        assert painted == "80% (3h 0m)  64% (2h 0m)"

    def test_the_tooltip_stacks_both_providers(self, monkeypatch):
        segments, _painted = self._painted(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()}
            ),
            httpx.Response(
                200,
                json=self._codex_body(
                    36.0, int((NOW + timedelta(hours=2)).timestamp())
                ),
                request=httpx.Request("GET", "https://example.test"),
            ),
        )

        assert len(segments) == 2

    def test_one_provider_failing_does_not_blank_the_other(self, monkeypatch):
        # Codex 401s while Claude is perfectly healthy: the label must still
        # carry Claude's real numbers beside Codex's error.
        segments, painted = self._painted(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()}
            ),
            httpx.Response(401, request=httpx.Request("GET", "https://example.test")),
        )

        assert [s.text for s in segments] == ["80% (3h 0m)", "token expired"]
        assert "80% (3h 0m)" in painted

    def test_an_unstarted_codex_window_reads_honestly(self, monkeypatch):
        segments, _painted = self._painted(
            monkeypatch,
            _usage_response(
                {"utilization": 20.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()}
            ),
            httpx.Response(
                200,
                json=self._codex_body(0, None),
                request=httpx.Request("GET", "https://example.test"),
            ),
        )

        assert segments[1].text == "100% (not started)"
