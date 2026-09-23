"""The one usage request, and the ladder of ways it can fail.

Both providers ask a different URL, sign it differently, and read a differently
shaped body — and nothing else about the two fetches differs. Each of them once
owned a copy of everything below, and the copies drifted: Claude read a 403 as
an outage, so the CLI was never nudged to sign in again, and Claude turned a
body that was not JSON into an unnamed internal error while Codex called it a
bad response.

The central rule is here rather than in either caller: **nothing raises across
this boundary**. Every failure comes back as a ``ProviderUsageData`` carrying a
``FetchError``, because the poll loop runs on a thread whose death would be
invisible in a windowed build.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from .models import ProviderUsageData, UsageWindow

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10.0

# The backend rejects a stale session with either code, and both mean the same
# thing to the user: the provider's CLI has to sign in again.
_EXPIRED_TOKEN_STATUS_CODES = (401, 403)
_RATE_LIMITED_STATUS_CODE = 429

# What one provider's usage reads as: the five-hour window and the weekly one,
# either of which the provider may not report.
UsageWindows = tuple[UsageWindow | None, UsageWindow | None]


@dataclass(frozen=True)
class UsageEndpoint:
    """Everything one provider's usage request differs by.

    ``expires_at`` is what the credentials claim, so a request certain to be
    refused is never sent. ``parse`` reads the provider's own body shape and
    may raise: that is how a body we cannot read becomes ``bad_response``
    rather than an invented number.
    """

    url: str
    headers: dict[str, str]
    expires_at: datetime | None
    parse: Callable[[dict], UsageWindows]


def _parse_retry_after_seconds(response) -> int | None:
    """Return a positive Retry-After value in seconds, or None when unusable."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def fetch_usage(
    open_endpoint: Callable[[], UsageEndpoint],
    *,
    named: str,
    now: datetime | None = None,
    get: Callable[..., httpx.Response] | None = None,
) -> ProviderUsageData:
    """Ask one provider for its usage, encoding every failure as data.

    ``open_endpoint`` reads the provider's credentials and says where to ask.
    It is a callable rather than a value because reading credentials is itself
    a step that fails, and its failures belong on the same ladder as the
    request's. ``named`` is how the provider is written in the log.
    """
    # Resolved here rather than as a default argument, which would bind
    # httpx.get at import and leave nothing for a test to replace.
    request = get or httpx.get
    now = now or datetime.now(timezone.utc)

    try:
        endpoint = open_endpoint()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        log.warning("%s credentials unavailable: %s", named, exc)
        return ProviderUsageData(fetch_error="no_credentials", fetched_at=now)
    except Exception as exc:
        log.warning("unexpected error reading %s credentials: %r", named, exc)
        return ProviderUsageData(fetch_error="unknown", fetched_at=now)

    if endpoint.expires_at is not None and endpoint.expires_at <= now:
        log.warning(
            "%s token expired at %s — skipping request until the CLI refreshes it",
            named,
            endpoint.expires_at.isoformat(),
        )
        return ProviderUsageData(fetch_error="token_expired", fetched_at=now)

    try:
        response = request(
            endpoint.url,
            headers=endpoint.headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in _EXPIRED_TOKEN_STATUS_CODES:
            log.warning("%s API returned %s — token expired", named, response.status_code)
            return ProviderUsageData(
                fetch_error="token_expired",
                fetched_at=now,
                status_code=response.status_code,
            )
        if response.status_code == _RATE_LIMITED_STATUS_CODE:
            retry_after = _parse_retry_after_seconds(response)
            log.warning(
                "%s API returned 429 — rate limited (retry-after=%s)", named, retry_after
            )
            return ProviderUsageData(
                fetch_error="rate_limited",
                fetched_at=now,
                status_code=response.status_code,
                retry_after_seconds=retry_after,
            )
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        log.warning("%s fetch timed out: %s", named, exc)
        return ProviderUsageData(fetch_error="timeout", fetched_at=now)
    except httpx.HTTPError as exc:
        log.warning("%s fetch failed (network/HTTP): %s", named, exc)
        status_code = None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            status_code = exc.response.status_code
        return ProviderUsageData(
            fetch_error="offline", fetched_at=now, status_code=status_code
        )
    except ValueError as exc:
        # response.json() on a body that is not JSON at all.
        log.warning("%s API returned an unreadable body: %r", named, exc)
        return ProviderUsageData(
            fetch_error="bad_response", fetched_at=now, status_code=response.status_code
        )
    except Exception as exc:
        log.warning("unexpected %s fetch error: %r", named, exc)
        return ProviderUsageData(fetch_error="unknown", fetched_at=now)

    try:
        five_hour, seven_day = endpoint.parse(body)
    except Exception as exc:
        log.warning("unexpected %s response shape: %r — body: %r", named, exc, body)
        return ProviderUsageData(
            fetch_error="bad_response", fetched_at=now, status_code=response.status_code
        )

    log.info(
        "fetched %s 5h=%s%% 7d=%s%%",
        named,
        f"{five_hour.utilization:.1f}" if five_hour else "N/A",
        f"{seven_day.utilization:.1f}" if seven_day else "N/A",
    )
    return ProviderUsageData(
        five_hour=five_hour,
        seven_day=seven_day,
        fetched_at=now,
        status_code=response.status_code,
    )
