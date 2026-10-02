"""Tests for the one usage request both providers make.

Every way a fetch can fail is decided here, once, so both providers answer a
403 the same way and both turn an unreadable body into ``bad_response``. They
did not, while each fetcher owned a copy of this ladder.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claudemonitor import usage_request
from claudemonitor.models import ProviderUsageData, UsageWindow
from claudemonitor.usage_request import UsageEndpoint, fetch_usage

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResponse:
    """Stand in for httpx.Response, covering only what the request touches."""

    def __init__(self, status_code: int, json_body=None, headers: dict | None = None):
        self.status_code = status_code
        self._json = {} if json_body is None else json_body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def _windows(body: dict):
    """Read a body the tests write themselves: two plain percentages."""
    return (
        UsageWindow(utilization=body["five"], resets_at=None),
        UsageWindow(utilization=body["seven"], resets_at=None),
    )


def _endpoint(expires_at: datetime | None = None, parse=_windows) -> UsageEndpoint:
    return UsageEndpoint(
        url="https://example.test/usage",
        headers={"Authorization": "Bearer tok"},
        expires_at=expires_at,
        parse=parse,
    )


def _fetch(response=None, *, endpoint=None, get=None, **kwargs) -> ProviderUsageData:
    """Run one fetch against a canned response or a canned failure."""
    if get is None:
        def get(url, headers, timeout):
            if isinstance(response, Exception):
                raise response
            return response

    return fetch_usage(
        lambda: endpoint if endpoint is not None else _endpoint(),
        named="claude",
        now=NOW,
        get=get,
        **kwargs,
    )


def _must_not_be_called(*args, **kwargs):
    raise AssertionError("no request should have been made")


# ------------------------------------------------------------------- success


class TestASuccessfulFetch:
    """A 200 with a readable body is the only case that produces usage."""

    def test_both_windows_come_back(self):
        data = _fetch(_FakeResponse(200, {"five": 30.0, "seven": 10.0}))

        assert data.five_hour.utilization == 30.0
        assert data.seven_day.utilization == 10.0

    def test_nothing_is_reported_as_an_error(self):
        data = _fetch(_FakeResponse(200, {"five": 30.0, "seven": 10.0}))

        assert data.fetch_error is None
        assert data.status_code == 200

    def test_the_fetch_is_stamped_with_the_time_it_was_made(self):
        assert _fetch(_FakeResponse(200, {"five": 0.0, "seven": 0.0})).fetched_at == NOW

    def test_the_endpoint_is_asked_for_exactly_what_it_named(self):
        asked: list[tuple] = []

        def get(url, headers, timeout):
            asked.append((url, headers))
            return _FakeResponse(200, {"five": 0.0, "seven": 0.0})

        _fetch(get=get)

        assert asked == [("https://example.test/usage", {"Authorization": "Bearer tok"})]

    def test_the_log_line_names_the_provider(self, caplog):
        with caplog.at_level(logging.INFO):
            _fetch(_FakeResponse(200, {"five": 30.0, "seven": 10.0}))

        assert "fetched claude" in caplog.text


# ---------------------------------------------------------------- credentials


class TestReadingCredentials:
    """Nothing is asked of the network until the credentials are in hand."""

    @pytest.mark.parametrize(
        "raised",
        [FileNotFoundError(), KeyError("tokens"), json.JSONDecodeError("bad", "{", 0)],
    )
    def test_an_unusable_credentials_file_reads_as_not_logged_in(self, raised):
        def endpoint():
            raise raised

        data = fetch_usage(endpoint, named="claude", now=NOW, get=_must_not_be_called)

        assert data.fetch_error == "no_credentials"

    def test_anything_else_is_reported_as_unknown_rather_than_raised(self):
        def endpoint():
            raise RuntimeError("something else")

        data = fetch_usage(endpoint, named="claude", now=NOW, get=_must_not_be_called)

        assert data.fetch_error == "unknown"

    def test_the_detail_of_an_unknown_failure_reaches_the_log(self, caplog):
        def endpoint():
            raise RuntimeError("something else")

        with caplog.at_level(logging.WARNING):
            fetch_usage(endpoint, named="claude", now=NOW, get=_must_not_be_called)

        assert "something else" in caplog.text

    def test_an_expired_token_skips_the_request_entirely(self):
        data = fetch_usage(
            lambda: _endpoint(expires_at=NOW - timedelta(minutes=5)),
            named="claude",
            now=NOW,
            get=_must_not_be_called,
        )

        assert data.fetch_error == "token_expired"
        assert data.status_code is None

    def test_a_token_that_expires_later_is_used(self):
        data = _fetch(
            _FakeResponse(200, {"five": 0.0, "seven": 0.0}),
            endpoint=_endpoint(expires_at=NOW + timedelta(hours=1)),
        )

        assert data.status_code == 200

    def test_a_token_with_no_stated_expiry_is_used(self):
        assert _fetch(_FakeResponse(200, {"five": 0.0, "seven": 0.0})).status_code == 200


# -------------------------------------------------------- what the server says


class TestWhatTheServerAnswers:
    """One ladder of status codes, so both providers read them the same way."""

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_a_refused_session_is_an_expired_token(self, status_code):
        # Both codes mean the same thing to the user: the CLI has to sign in
        # again. Claude answered 403 with "offline", so no nudge ever ran.
        data = _fetch(_FakeResponse(status_code))

        assert data.fetch_error == "token_expired"
        assert data.status_code == status_code

    def test_too_many_requests_is_a_rate_limit_rather_than_an_outage(self):
        # 429 is >= 400, so without its own rung it would read as "offline"
        # and the last good usage would be thrown away.
        data = _fetch(_FakeResponse(429))

        assert data.fetch_error == "rate_limited"
        assert data.status_code == 429

    @pytest.mark.parametrize(
        ("header", "expected"),
        [("224", 224), ("0", None), ("-5", None), ("abc", None)],
    )
    def test_the_retry_after_header_is_honoured_only_when_it_is_usable(
        self, header, expected
    ):
        data = _fetch(_FakeResponse(429, headers={"retry-after": header}))

        assert data.retry_after_seconds == expected

    def test_a_rate_limit_with_no_retry_after_leaves_the_backoff_to_us(self):
        assert _fetch(_FakeResponse(429)).retry_after_seconds is None

    def test_any_other_failing_status_is_an_outage_that_keeps_its_code(self):
        data = _fetch(_FakeResponse(503))

        assert data.fetch_error == "offline"
        assert data.status_code == 503

    def test_a_body_that_is_not_json_at_all_is_a_bad_response(self):
        data = _fetch(_FakeResponse(200, json_body=ValueError("not json")))

        assert data.fetch_error == "bad_response"
        assert data.status_code == 200

    def test_a_body_shaped_wrongly_is_a_bad_response(self):
        data = _fetch(_FakeResponse(200, {"unexpected": "shape"}))

        assert data.fetch_error == "bad_response"
        assert data.status_code == 200

    def test_the_body_reaches_the_log_when_it_cannot_be_read(self, caplog):
        with caplog.at_level(logging.WARNING):
            _fetch(_FakeResponse(200, {"unexpected": "shape"}))

        assert "unexpected" in caplog.text


class TestWhenTheNetworkFails:
    """A fetcher never raises, so every transport failure comes back as data."""

    def test_a_timeout_is_told_apart_from_an_outage(self):
        data = _fetch(httpx.TimeoutException("too slow"))

        assert data.fetch_error == "timeout"

    def test_an_unreachable_host_is_an_outage_with_no_status_code(self):
        data = _fetch(httpx.ConnectError("no route"))

        assert data.fetch_error == "offline"
        assert data.status_code is None

    def test_an_unforeseen_failure_is_reported_as_unknown(self):
        data = _fetch(RuntimeError("something else"))

        assert data.fetch_error == "unknown"

    def test_nothing_escapes_the_fetch(self):
        # The poll loop runs on a thread whose death would be invisible.
        assert isinstance(_fetch(RuntimeError("boom")), ProviderUsageData)


class TestRetryAfterParsing:
    """The header is advisory, so only a positive whole number is obeyed."""

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after": "224"}, 224),
            ({"retry-after": "0"}, None),
            ({"retry-after": "-5"}, None),
            ({"retry-after": "abc"}, None),
            ({}, None),
        ],
    )
    def test_each_variant(self, headers, expected):
        assert (
            usage_request._parse_retry_after_seconds(_FakeResponse(429, headers=headers))
            == expected
        )
