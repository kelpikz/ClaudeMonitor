from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claudemonitor import fetcher
from claudemonitor.models import AnthropicUsageData


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
        # fetch() returns before this for the status codes it special-cases, so
        # this only matters for codes we don't map explicitly.
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


@pytest.fixture
def fake_token(monkeypatch):
    """Skip real credential reading — every fetch test wants a valid token."""
    monkeypatch.setattr(
        fetcher, "_read_credentials", lambda: fetcher.Credentials("test-token", None)
    )


def _fail_if_called(*args, **kwargs):
    raise AssertionError("fetch() must not hit the network with an expired token")


def test_429_maps_to_rate_limited(fake_token, monkeypatch):
    # The whole point of step 1: a 429 must be encoded as "rate_limited" so the
    # processor can fall back to the last good data instead of going grey.
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(429))
    data = fetcher.fetch()
    assert isinstance(data, AnthropicUsageData)
    assert data.fetch_error == "rate_limited"
    assert data.status_code == 429
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
    assert data.status_code == 200
    assert data.five_hour is not None and data.five_hour.utilization == 30.0


def test_401_records_status_code(fake_token, monkeypatch):
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(401))
    data = fetcher.fetch()
    assert data.fetch_error == "token_expired"
    assert data.status_code == 401


def test_429_captures_positive_retry_after_header(fake_token, monkeypatch):
    response = _FakeResponse(429, headers={"retry-after": "224", "server": "cloudflare"})
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: response)
    data = fetcher.fetch()
    assert data.retry_after_seconds == 224


def test_429_ignores_zero_retry_after_header(fake_token, monkeypatch):
    response = _FakeResponse(429, headers={"retry-after": "0", "server": "cloudflare"})
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: response)
    data = fetcher.fetch()
    assert data.retry_after_seconds is None


def test_429_without_retry_after_header_leaves_none(fake_token, monkeypatch):
    response = _FakeResponse(429, headers={"server": "cloudflare"})
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: response)
    data = fetcher.fetch()
    assert data.retry_after_seconds is None


def test_parse_retry_after_seconds_handles_variants():
    class _Headered:
        def __init__(self, headers):
            self.headers = headers

    assert fetcher._parse_retry_after_seconds(_Headered({"retry-after": "224"})) == 224
    assert fetcher._parse_retry_after_seconds(_Headered({"retry-after": "0"})) is None
    assert fetcher._parse_retry_after_seconds(_Headered({"retry-after": "-5"})) is None
    assert fetcher._parse_retry_after_seconds(_Headered({"retry-after": "abc"})) is None
    assert fetcher._parse_retry_after_seconds(_Headered({})) is None


def test_expired_token_skips_network_request(monkeypatch):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    monkeypatch.setattr(
        fetcher,
        "_read_credentials",
        lambda: fetcher.Credentials("test-token", expired_at),
    )
    monkeypatch.setattr(fetcher.httpx, "get", _fail_if_called)

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
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200))

    assert fetcher.fetch().status_code == 200


def test_missing_expiry_still_makes_network_request(fake_token, monkeypatch):
    monkeypatch.setattr(fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200))

    assert fetcher.fetch().status_code == 200


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
