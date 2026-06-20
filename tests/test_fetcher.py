from __future__ import annotations

import httpx
import pytest

from claudemonitor import fetcher
from claudemonitor.models import AnthropicUsageData


class _FakeResponse:
    """Minimal stand-in for httpx.Response covering only what fetch() touches."""

    def __init__(self, status_code: int, json_body: dict | None = None):
        self.status_code = status_code
        self._json = json_body or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        # fetch() returns before this for the status codes it special-cases, so
        # this only matters for codes we don't map explicitly.
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


@pytest.fixture
def fake_token(monkeypatch):
    """Skip real credential reading — every fetch test wants a valid token."""
    monkeypatch.setattr(fetcher, "_read_token", lambda: "test-token")


def test_429_maps_to_rate_limited(fake_token, monkeypatch):
    # The whole point of step 1: a 429 must be encoded as "rate_limited" so the
    # processor can fall back to the last good data instead of going grey.
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(429))
    data = fetcher.fetch()
    assert isinstance(data, AnthropicUsageData)
    assert data.fetch_error == "rate_limited"
    assert data.five_hour is None
    assert data.seven_day is None


def test_429_check_precedes_generic_http_error(fake_token, monkeypatch):
    # 429 is >= 400, so without an explicit branch it would fall through to the
    # generic "offline" mapping. Pin that it does not.
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(429))
    assert fetcher.fetch().fetch_error == "rate_limited"


def test_200_with_windows_has_no_error(fake_token, monkeypatch):
    body = {
        "five_hour": {"utilization": 30.0, "resets_at": "2026-06-20T14:00:00Z"},
        "seven_day": {"utilization": 10.0, "resets_at": "2026-06-23T14:00:00Z"},
    }
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, body))
    data = fetcher.fetch()
    assert data.fetch_error is None
    assert data.five_hour is not None and data.five_hour.utilization == 30.0
