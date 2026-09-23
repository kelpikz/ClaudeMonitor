"""Tests for the Claude usage fetcher.

The end-to-end path here runs from "bytes the Anthropic endpoint returned" to
"ProviderUsageData the processor can format". What is asserted is what is
Claude's own: its credentials file, the URL it asks, and how its body maps onto
the shared model. Every way the request itself can fail is one ladder shared
with Codex, and is pinned in ``test_usage_request`` rather than twice over here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claudemonitor import fetcher, usage_request
from claudemonitor.models import ProviderUsageData


class _FakeResponse:
    """Minimal stand-in for httpx.Response covering only what fetch() touches."""

    def __init__(
        self,
        status_code: int,
        json_body: dict | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_token(monkeypatch):
    """Skip real credential reading — every fetch test wants a valid token."""
    monkeypatch.setattr(
        fetcher, "_read_credentials", lambda: fetcher.Credentials("test-token", None)
    )


def _answers(monkeypatch, response) -> None:
    """Make the one shared request return a canned response."""
    monkeypatch.setattr(usage_request.httpx, "get", lambda *a, **k: response)


def _fail_if_called(*args, **kwargs):
    raise AssertionError("fetch() must not hit the network with an expired token")


_USAGE_BODY = {
    "five_hour": {"utilization": 30.0, "resets_at": "2026-06-20T14:00:00Z"},
    "seven_day": {"utilization": 10.0, "resets_at": "2026-06-23T14:00:00Z"},
}


# --------------------------------------------------------------------------
# End-to-end: response bytes -> ProviderUsageData
# --------------------------------------------------------------------------


def test_happy_path_maps_both_windows(fake_token, monkeypatch):
    _answers(monkeypatch, _FakeResponse(200, _USAGE_BODY))

    data = fetcher.fetch()

    assert isinstance(data, ProviderUsageData)
    assert data.fetch_error is None
    assert data.status_code == 200
    assert data.five_hour.utilization == 30.0
    assert data.seven_day.utilization == 10.0


def test_request_targets_the_usage_endpoint_with_auth_headers(fake_token, monkeypatch):
    captured: dict = {}

    def _capture(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(200, _USAGE_BODY)

    monkeypatch.setattr(usage_request.httpx, "get", _capture)

    fetcher.fetch()

    assert captured["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["anthropic-beta"] == "oauth-2025-04-20"


def test_a_body_with_no_windows_reports_neither(fake_token, monkeypatch):
    _answers(monkeypatch, _FakeResponse(200, {}))

    data = fetcher.fetch()

    assert data.fetch_error is None
    assert data.five_hour is None and data.seven_day is None


def test_a_refused_session_reaches_the_caller_as_an_expired_token(
    fake_token, monkeypatch
):
    # One rung of the shared ladder, end to end, so the wiring is proven too.
    _answers(monkeypatch, _FakeResponse(401))

    data = fetcher.fetch()

    assert data.fetch_error == "token_expired"
    assert data.status_code == 401


def test_too_many_requests_keeps_its_retry_after(fake_token, monkeypatch):
    _answers(monkeypatch, _FakeResponse(429, headers={"retry-after": "224"}))

    data = fetcher.fetch()

    assert data.fetch_error == "rate_limited"
    assert data.retry_after_seconds == 224


def test_wrongly_shaped_window_maps_to_bad_response(fake_token, monkeypatch):
    _answers(monkeypatch, _FakeResponse(200, {"five_hour": {"utilization": "lots"}}))

    data = fetcher.fetch()

    assert data.fetch_error == "bad_response"
    assert data.status_code == 200


# --------------------------------------------------------------------------
# End-to-end: credential problems
# --------------------------------------------------------------------------


def test_expired_token_skips_network_request(monkeypatch):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    monkeypatch.setattr(
        fetcher,
        "_read_credentials",
        lambda: fetcher.Credentials("test-token", expired_at),
    )
    monkeypatch.setattr(usage_request.httpx, "get", _fail_if_called)

    data = fetcher.fetch()

    assert data.fetch_error == "token_expired"
    assert data.status_code is None


def test_unexpired_token_makes_network_request(monkeypatch):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(
        fetcher,
        "_read_credentials",
        lambda: fetcher.Credentials("test-token", expires_at),
    )
    _answers(monkeypatch, _FakeResponse(200, _USAGE_BODY))

    assert fetcher.fetch().status_code == 200


def test_missing_expiry_still_makes_network_request(fake_token, monkeypatch):
    _answers(monkeypatch, _FakeResponse(200, _USAGE_BODY))

    assert fetcher.fetch().status_code == 200


def test_a_missing_credentials_file_maps_to_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "_CREDENTIALS_FILE", tmp_path / ".credentials.json")
    monkeypatch.setattr(usage_request.httpx, "get", _fail_if_called)

    assert fetcher.fetch().fetch_error == "no_credentials"


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def test_credentials_parse_reads_token_and_expiry():
    payload = {
        "claudeAiOauth": {
            "accessToken": "tok",
            "refreshToken": "ref",
            "expiresAt": 1752161234567,
        }
    }

    creds = fetcher._parse_credentials(payload)

    assert creds.access_token == "tok"
    assert creds.expires_at == datetime.fromtimestamp(
        1752161234567 / 1000, tz=timezone.utc
    )


def test_credentials_parse_tolerates_missing_expiry():
    payload = {"claudeAiOauth": {"accessToken": "tok"}}

    creds = fetcher._parse_credentials(payload)

    assert creds.access_token == "tok"
    assert creds.expires_at is None


def test_usage_windows_reads_both_windows():
    five_hour, seven_day = fetcher._usage_windows(_USAGE_BODY)

    assert five_hour.utilization == 30.0
    assert seven_day.utilization == 10.0


def test_usage_windows_of_an_empty_body_is_a_pair_of_nothing():
    assert fetcher._usage_windows({}) == (None, None)


def test_usage_windows_raises_on_a_window_it_cannot_read():
    # The shared request turns this into bad_response rather than a number.
    with pytest.raises(Exception):
        fetcher._usage_windows({"five_hour": {"utilization": "lots"}})


def test_the_endpoint_is_built_from_the_credentials(monkeypatch):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(
        fetcher, "_read_credentials", lambda: fetcher.Credentials("tok", expires_at)
    )

    endpoint = fetcher._endpoint()

    assert endpoint.url == "https://api.anthropic.com/api/oauth/usage"
    assert endpoint.headers["Authorization"] == "Bearer tok"
    assert endpoint.expires_at == expires_at
