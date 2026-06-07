from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .models import AnthropicUsageData, UsageWindow

log = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


def _read_token() -> str:
    data = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    return data["claudeAiOauth"]["accessToken"]


def fetch() -> AnthropicUsageData:
    now = datetime.now(timezone.utc)
    try:
        token = _read_token()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        log.warning("credentials unavailable: %s", exc)
        return AnthropicUsageData(fetch_error="no_credentials", fetched_at=now)
    except Exception as exc:
        log.warning("unexpected error reading credentials: %r", exc)
        return AnthropicUsageData(fetch_error=f"unknown: {exc!r}", fetched_at=now)

    try:
        response = httpx.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
            timeout=10.0,
        )
        if response.status_code == 401:
            log.warning("API returned 401 — token expired")
            return AnthropicUsageData(fetch_error="token_expired", fetched_at=now)
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        log.warning("fetch timed out: %s", exc)
        return AnthropicUsageData(fetch_error="timeout", fetched_at=now)
    except httpx.HTTPError as exc:
        log.warning("fetch failed (network/HTTP): %s", exc)
        return AnthropicUsageData(fetch_error="offline", fetched_at=now)
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
        return AnthropicUsageData(fetch_error="bad_response", fetched_at=now)

    log.info(
        "fetched 5h=%s%% 7d=%s%%",
        f"{five_hour.utilization:.1f}" if five_hour else "N/A",
        f"{seven_day.utilization:.1f}" if seven_day else "N/A",
    )
    return AnthropicUsageData(five_hour=five_hour, seven_day=seven_day, fetched_at=now)
