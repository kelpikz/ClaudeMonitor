from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .models import AnthropicUsageData, UsageWindow

log = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


@dataclass(frozen=True)
class Credentials:
    access_token: str
    expires_at: datetime | None


def _parse_credentials(payload: dict) -> Credentials:
    """Extract the access token and its expiry from the credentials file payload."""
    oauth = payload["claudeAiOauth"]
    expires_at_ms = oauth.get("expiresAt")
    expires_at = (
        datetime.fromtimestamp(expires_at_ms / 1000, tz=timezone.utc)
        if expires_at_ms is not None
        else None
    )
    return Credentials(access_token=oauth["accessToken"], expires_at=expires_at)


def _read_credentials() -> Credentials:
    return _parse_credentials(json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8")))


def _parse_retry_after_seconds(response: httpx.Response) -> int | None:
    """Return a positive Retry-After value in seconds, or None when absent/unusable."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def fetch() -> AnthropicUsageData:
    now = datetime.now(timezone.utc)
    try:
        credentials = _read_credentials()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        log.warning("credentials unavailable: %s", exc)
        return AnthropicUsageData(fetch_error="no_credentials", fetched_at=now)
    except Exception as exc:
        log.warning("unexpected error reading credentials: %r", exc)
        return AnthropicUsageData(fetch_error=f"unknown: {exc!r}", fetched_at=now)

    if credentials.expires_at is not None and credentials.expires_at <= now:
        log.warning(
            "token expired at %s — skipping request until Claude Code refreshes it",
            credentials.expires_at.isoformat(),
        )
        return AnthropicUsageData(fetch_error="token_expired", fetched_at=now)

    try:
        response = httpx.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=10.0,
        )
        if response.status_code == 401:
            log.warning("API returned 401 — token expired")
            return AnthropicUsageData(
                fetch_error="token_expired",
                fetched_at=now,
                status_code=response.status_code,
            )
        if response.status_code == 429:
            retry_after = _parse_retry_after_seconds(response)
            log.warning("API returned 429 — rate limited (retry-after=%s)", retry_after)
            return AnthropicUsageData(
                fetch_error="rate_limited",
                fetched_at=now,
                status_code=response.status_code,
                retry_after_seconds=retry_after,
            )
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        log.warning("fetch timed out: %s", exc)
        return AnthropicUsageData(fetch_error="timeout", fetched_at=now)
    except httpx.HTTPError as exc:
        log.warning("fetch failed (network/HTTP): %s", exc)
        status_code = None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            status_code = exc.response.status_code
        return AnthropicUsageData(
            fetch_error="offline",
            fetched_at=now,
            status_code=status_code,
        )
    except Exception as exc:
        log.warning("unexpected fetch error: %r", exc)
        return AnthropicUsageData(fetch_error=f"unknown: {exc!r}", fetched_at=now)

    five_hour_raw = body.get("five_hour")
    seven_day_raw = body.get("seven_day")

    try:
        five_hour = UsageWindow(**five_hour_raw) if five_hour_raw else None
        seven_day = UsageWindow(**seven_day_raw) if seven_day_raw else None
    except Exception as exc:
        log.warning("unexpected API response shape: %r — body: %r", exc, body)
        return AnthropicUsageData(
            fetch_error="bad_response",
            fetched_at=now,
            status_code=response.status_code,
        )

    log.info(
        "fetched 5h=%s%% 7d=%s%%",
        f"{five_hour.utilization:.1f}" if five_hour else "N/A",
        f"{seven_day.utilization:.1f}" if seven_day else "N/A",
    )
    return AnthropicUsageData(
        five_hour=five_hour,
        seven_day=seven_day,
        fetched_at=now,
        status_code=response.status_code,
    )
