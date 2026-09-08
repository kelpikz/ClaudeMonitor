"""Read OpenAI Codex usage from the ChatGPT backend.

Mirrors ``fetcher.py`` for the second provider, including its central rule:
every failure comes back as a ``ProviderUsageData`` with ``fetch_error`` set,
so nothing raises across the module boundary and the poll loop cannot die.

Credentials come from the Codex CLI's own ``auth.json``. This module only ever
reads that file — refreshing an expired token is the CLI's job, and
``cli_refresher.py`` asks it to do so.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .models import ProviderUsageData, UsageWindow

log = logging.getLogger(__name__)

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_USER_AGENT = "ClaudeMonitor/0.1"

# The ChatGPT backend rejects a stale session with either code, and both mean
# the same thing to the user: the Codex CLI has to sign in again.
_EXPIRED_TOKEN_STATUS_CODES = (401, 403)


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str
    expires_at: datetime | None


def _auth_path() -> Path:
    """Locate the Codex CLI's credential file, honoring a CODEX_HOME override."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _token_expiry(access_token: str) -> datetime | None:
    """Read the ``exp`` claim out of a JWT access token without verifying it.

    Only the expiry is wanted, and only to skip a request that is certain to
    fail. An opaque or malformed token is not an error here: the request still
    goes out, and the server's 401 becomes the source of truth instead.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
        expires_at = claims["exp"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        return None
    try:
        return datetime.fromtimestamp(expires_at, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _parse_credentials(payload: dict) -> CodexCredentials:
    """Extract the access token, account id, and expiry from auth.json."""
    tokens = payload["tokens"]
    access_token = tokens["access_token"]
    return CodexCredentials(
        access_token=access_token,
        account_id=tokens.get("account_id", ""),
        expires_at=_token_expiry(access_token),
    )


def _read_credentials() -> CodexCredentials:
    return _parse_credentials(json.loads(_auth_path().read_text(encoding="utf-8")))


def _parse_retry_after_seconds(response: httpx.Response) -> int | None:
    """Return a positive Retry-After value in seconds, or None when unusable."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _reset_time(reset_at: float | int | str | None) -> datetime | None:
    """Convert the window's Unix-seconds reset stamp into an aware datetime."""
    if reset_at is None:
        return None
    return datetime.fromtimestamp(float(reset_at), tz=timezone.utc)


def _usage_window(raw: dict | None) -> UsageWindow | None:
    """Map one Codex rate-limit window onto the shared UsageWindow model.

    Raises on a wrongly shaped window so the caller can report ``bad_response``
    rather than showing an invented number.
    """
    if not raw:
        return None
    return UsageWindow(
        utilization=float(raw["used_percent"]),
        resets_at=_reset_time(raw.get("reset_at")),
    )


def fetch() -> ProviderUsageData:
    """Return current Codex usage, encoding every failure as data."""
    now = datetime.now(timezone.utc)
    try:
        credentials = _read_credentials()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        log.warning("codex credentials unavailable: %s", exc)
        return ProviderUsageData(fetch_error="no_credentials", fetched_at=now)
    except Exception as exc:
        log.warning("unexpected error reading codex credentials: %r", exc)
        return ProviderUsageData(fetch_error=f"unknown: {exc!r}", fetched_at=now)

    if credentials.expires_at is not None and credentials.expires_at <= now:
        log.warning(
            "codex token expired at %s — skipping request until Codex refreshes it",
            credentials.expires_at.isoformat(),
        )
        return ProviderUsageData(fetch_error="token_expired", fetched_at=now)

    try:
        response = httpx.get(
            _USAGE_URL,
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "ChatGPT-Account-Id": credentials.account_id,
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
            },
            timeout=10.0,
        )
        if response.status_code in _EXPIRED_TOKEN_STATUS_CODES:
            log.warning("codex API returned %s — token expired", response.status_code)
            return ProviderUsageData(
                fetch_error="token_expired",
                fetched_at=now,
                status_code=response.status_code,
            )
        if response.status_code == 429:
            retry_after = _parse_retry_after_seconds(response)
            log.warning(
                "codex API returned 429 — rate limited (retry-after=%s)", retry_after
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
        log.warning("codex fetch timed out: %s", exc)
        return ProviderUsageData(fetch_error="timeout", fetched_at=now)
    except httpx.HTTPError as exc:
        log.warning("codex fetch failed (network/HTTP): %s", exc)
        status_code = None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            status_code = exc.response.status_code
        return ProviderUsageData(
            fetch_error="offline",
            fetched_at=now,
            status_code=status_code,
        )
    except ValueError as exc:
        # response.json() on a body that is not JSON at all.
        log.warning("codex API returned an unreadable body: %r", exc)
        return ProviderUsageData(
            fetch_error="bad_response",
            fetched_at=now,
            status_code=response.status_code,
        )
    except Exception as exc:
        log.warning("unexpected codex fetch error: %r", exc)
        return ProviderUsageData(fetch_error=f"unknown: {exc!r}", fetched_at=now)

    rate_limit = body.get("rate_limit") or {}
    try:
        five_hour = _usage_window(rate_limit.get("primary_window"))
        seven_day = _usage_window(rate_limit.get("secondary_window"))
    except Exception as exc:
        log.warning("unexpected codex response shape: %r — body: %r", exc, body)
        return ProviderUsageData(
            fetch_error="bad_response",
            fetched_at=now,
            status_code=response.status_code,
        )

    log.info(
        "fetched codex 5h=%s%% 7d=%s%%",
        f"{five_hour.utilization:.1f}" if five_hour else "N/A",
        f"{seven_day.utilization:.1f}" if seven_day else "N/A",
    )
    return ProviderUsageData(
        five_hour=five_hour,
        seven_day=seven_day,
        fetched_at=now,
        status_code=response.status_code,
    )
