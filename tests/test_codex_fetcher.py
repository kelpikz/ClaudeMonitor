"""Tests for the Codex usage fetcher.

The end-to-end path here runs from "bytes the ChatGPT backend returned" to
"ProviderUsageData the processor can format", including every failure the
network and the credential file can produce.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claudemonitor import codex_fetcher
from claudemonitor.models import ProviderUsageData

# One real reset moment, reused so assertions read as the same instant.
_PRIMARY_RESET_UNIX = 1787860125
_SECONDARY_RESET_UNIX = 1788272951


def _usage_body(
    primary: dict | None = None,
    secondary: dict | None = None,
    plan_type: str = "plus",
) -> dict:
    """Build a response body shaped like GET /backend-api/wham/usage."""
    return {
        "plan_type": plan_type,
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": primary
            if primary is not None
            else {
                "used_percent": 12.5,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 16882,
                "reset_at": _PRIMARY_RESET_UNIX,
            },
            "secondary_window": secondary
            if secondary is not None
            else {
                "used_percent": 64.0,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 429708,
                "reset_at": _SECONDARY_RESET_UNIX,
            },
        },
    }


def _jwt_expiring_at(expires_at: datetime | None) -> str:
    """Build an unsigned JWT whose payload carries the given expiry."""
    claims = {} if expires_at is None else {"exp": int(expires_at.timestamp())}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return "header." + payload.decode() + ".signature"


class _FakeResponse:
    """Minimal stand-in for httpx.Response covering only what fetch() touches."""

    def __init__(
        self,
        status_code: int,
        json_body: dict | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


@pytest.fixture
def fake_credentials(monkeypatch):
    """Supply a valid, unexpired Codex credential to every fetch test."""
    monkeypatch.setattr(
        codex_fetcher,
        "_read_credentials",
        lambda: codex_fetcher.CodexCredentials(
            access_token="test-token",
            account_id="test-account",
            expires_at=None,
        ),
    )


def _fail_if_called(*args, **kwargs):
    raise AssertionError("fetch() must not hit the network without a usable token")


# --------------------------------------------------------------------------
# End-to-end: response bytes -> ProviderUsageData
# --------------------------------------------------------------------------


def test_happy_path_maps_both_windows(fake_credentials, monkeypatch):
    monkeypatch.setattr(
        codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, _usage_body())
    )

    data = codex_fetcher.fetch()

    assert isinstance(data, ProviderUsageData)
    assert data.fetch_error is None
    assert data.status_code == 200
    assert data.five_hour is not None
    assert data.five_hour.utilization == 12.5
    assert data.five_hour.resets_at == datetime.fromtimestamp(
        _PRIMARY_RESET_UNIX, tz=timezone.utc
    )
    assert data.seven_day is not None
    assert data.seven_day.utilization == 64.0
    assert data.seven_day.resets_at == datetime.fromtimestamp(
        _SECONDARY_RESET_UNIX, tz=timezone.utc
    )


def test_request_targets_the_usage_endpoint_with_auth_headers(
    fake_credentials, monkeypatch
):
    captured: dict = {}

    def _capture(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeResponse(200, _usage_body())

    monkeypatch.setattr(codex_fetcher.httpx, "get", _capture)

    codex_fetcher.fetch()

    assert captured["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["headers"]["ChatGPT-Account-Id"] == "test-account"


def test_untouched_window_reports_zero_usage(fake_credentials, monkeypatch):
    # A brand-new window is real data, not an error: 0% used must survive as 0.0
    # so the processor can say "not started" rather than "no data".
    body = _usage_body(
        primary={
            "used_percent": 0,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 0,
            "reset_at": None,
        }
    )
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, body))

    data = codex_fetcher.fetch()

    assert data.fetch_error is None
    assert data.five_hour is not None
    assert data.five_hour.utilization == 0.0
    assert data.five_hour.resets_at is None


def test_missing_windows_map_to_none(fake_credentials, monkeypatch):
    body = _usage_body(primary=None, secondary=None)
    body["rate_limit"]["primary_window"] = None
    body["rate_limit"]["secondary_window"] = None
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, body))

    data = codex_fetcher.fetch()

    assert data.fetch_error is None
    assert data.five_hour is None
    assert data.seven_day is None


def test_absent_rate_limit_section_maps_to_none_windows(fake_credentials, monkeypatch):
    monkeypatch.setattr(
        codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, {"plan_type": "plus"})
    )

    data = codex_fetcher.fetch()

    assert data.fetch_error is None
    assert data.five_hour is None


# --------------------------------------------------------------------------
# End-to-end: failures become values, never exceptions
# --------------------------------------------------------------------------


def test_401_maps_to_token_expired(fake_credentials, monkeypatch):
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(401))

    data = codex_fetcher.fetch()

    assert data.fetch_error == "token_expired"
    assert data.status_code == 401


def test_403_maps_to_token_expired(fake_credentials, monkeypatch):
    # The ChatGPT backend answers a stale session with 403 as readily as 401,
    # and both mean the same thing to the user: sign the Codex CLI back in.
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(403))

    data = codex_fetcher.fetch()

    assert data.fetch_error == "token_expired"
    assert data.status_code == 403


def test_429_maps_to_rate_limited_with_retry_after(fake_credentials, monkeypatch):
    response = _FakeResponse(429, headers={"retry-after": "224"})
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: response)

    data = codex_fetcher.fetch()

    assert data.fetch_error == "rate_limited"
    assert data.status_code == 429
    assert data.retry_after_seconds == 224


def test_429_ignores_unusable_retry_after(fake_credentials, monkeypatch):
    response = _FakeResponse(429, headers={"retry-after": "soon"})
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: response)

    assert codex_fetcher.fetch().retry_after_seconds is None


def test_timeout_maps_to_timeout(fake_credentials, monkeypatch):
    def _timeout(*a, **k):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(codex_fetcher.httpx, "get", _timeout)

    assert codex_fetcher.fetch().fetch_error == "timeout"


def test_network_failure_maps_to_offline(fake_credentials, monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(codex_fetcher.httpx, "get", _boom)

    assert codex_fetcher.fetch().fetch_error == "offline"


def test_server_error_maps_to_offline(fake_credentials, monkeypatch):
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(500))

    assert codex_fetcher.fetch().fetch_error == "offline"


def test_unparseable_body_maps_to_bad_response(fake_credentials, monkeypatch):
    class _Undecodable(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _Undecodable(200))

    assert codex_fetcher.fetch().fetch_error == "bad_response"


def test_wrongly_shaped_window_maps_to_bad_response(fake_credentials, monkeypatch):
    body = _usage_body(primary={"used_percent": "lots", "reset_at": "whenever"})
    monkeypatch.setattr(codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, body))

    data = codex_fetcher.fetch()

    assert data.fetch_error == "bad_response"
    assert data.status_code == 200


def test_unexpected_error_is_still_returned_as_data(fake_credentials, monkeypatch):
    def _weird(*a, **k):
        raise RuntimeError("something else")

    monkeypatch.setattr(codex_fetcher.httpx, "get", _weird)

    data = codex_fetcher.fetch()

    assert data.fetch_error is not None
    assert data.fetch_error.startswith("unknown")


# --------------------------------------------------------------------------
# End-to-end: credential problems
# --------------------------------------------------------------------------


def test_missing_auth_file_maps_to_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_fetcher, "_auth_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(codex_fetcher.httpx, "get", _fail_if_called)

    data = codex_fetcher.fetch()

    assert data.fetch_error == "no_credentials"
    assert data.status_code is None


def test_malformed_auth_file_maps_to_no_credentials(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(codex_fetcher, "_auth_path", lambda: path)
    monkeypatch.setattr(codex_fetcher.httpx, "get", _fail_if_called)

    assert codex_fetcher.fetch().fetch_error == "no_credentials"


def test_auth_file_without_tokens_maps_to_no_credentials(monkeypatch, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"OPENAI_API_KEY": None}), encoding="utf-8")
    monkeypatch.setattr(codex_fetcher, "_auth_path", lambda: path)
    monkeypatch.setattr(codex_fetcher.httpx, "get", _fail_if_called)

    assert codex_fetcher.fetch().fetch_error == "no_credentials"


def test_expired_token_skips_the_network(monkeypatch):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    monkeypatch.setattr(
        codex_fetcher,
        "_read_credentials",
        lambda: codex_fetcher.CodexCredentials("tok", "acct", expired_at),
    )
    monkeypatch.setattr(codex_fetcher.httpx, "get", _fail_if_called)

    data = codex_fetcher.fetch()

    assert data.fetch_error == "token_expired"
    assert data.status_code is None


def test_unexpired_token_makes_the_request(monkeypatch):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(
        codex_fetcher,
        "_read_credentials",
        lambda: codex_fetcher.CodexCredentials("tok", "acct", expires_at),
    )
    monkeypatch.setattr(
        codex_fetcher.httpx, "get", lambda *a, **k: _FakeResponse(200, _usage_body())
    )

    assert codex_fetcher.fetch().status_code == 200


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def test_auth_path_prefers_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    assert codex_fetcher._auth_path() == tmp_path / "auth.json"


def test_auth_path_falls_back_to_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_fetcher.Path, "home", staticmethod(lambda: tmp_path))

    assert codex_fetcher._auth_path() == tmp_path / ".codex" / "auth.json"


def test_parse_credentials_reads_token_and_account():
    payload = {
        "tokens": {
            "access_token": _jwt_expiring_at(None),
            "account_id": "acct-1",
            "refresh_token": "ref",
        }
    }

    credentials = codex_fetcher._parse_credentials(payload)

    assert credentials.account_id == "acct-1"
    assert credentials.expires_at is None


def test_parse_credentials_reads_expiry_from_the_jwt():
    expires_at = datetime(2026, 9, 4, 16, 4, 44, tzinfo=timezone.utc)
    payload = {
        "tokens": {"access_token": _jwt_expiring_at(expires_at), "account_id": "acct-1"}
    }

    credentials = codex_fetcher._parse_credentials(payload)

    assert credentials.expires_at == expires_at


def test_parse_credentials_requires_an_access_token():
    with pytest.raises(KeyError):
        codex_fetcher._parse_credentials({"tokens": {"account_id": "acct-1"}})


def test_token_expiry_tolerates_a_token_that_is_not_a_jwt():
    # An opaque token is not an error — it just has no expiry we can read, so
    # the request goes out and a 401 becomes the source of truth instead.
    assert codex_fetcher._token_expiry("not-a-jwt") is None
    assert codex_fetcher._token_expiry("a.b.c") is None
    assert codex_fetcher._token_expiry("") is None


def test_usage_window_maps_percent_and_reset():
    window = codex_fetcher._usage_window(
        {"used_percent": 42, "reset_at": _PRIMARY_RESET_UNIX}
    )

    assert window is not None
    assert window.utilization == 42.0
    assert window.resets_at == datetime.fromtimestamp(_PRIMARY_RESET_UNIX, tz=timezone.utc)


def test_usage_window_of_nothing_is_none():
    assert codex_fetcher._usage_window(None) is None
