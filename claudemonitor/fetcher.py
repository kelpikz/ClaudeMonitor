"""Read Claude usage from the Anthropic OAuth endpoint.

Only what is Claude's own is here: where the credentials file is, what it
looks like, which URL to ask, and how to read the body that comes back.
Everything else — the request, the timeout, and every way it can fail — is in
``usage_request``, which both providers share.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ProviderUsageData, UsageWindow
from .usage_request import UsageEndpoint, UsageWindows, fetch_usage

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
_OAUTH_BETA = "oauth-2025-04-20"

PROVIDER_NAME = "claude"


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


def _usage_windows(body: dict) -> UsageWindows:
    """Map Anthropic's two windows onto the shared model.

    Raises on a wrongly shaped window, which the shared request reports as
    ``bad_response`` rather than showing an invented number.
    """
    five_hour_raw = body.get("five_hour")
    seven_day_raw = body.get("seven_day")
    return (
        UsageWindow(**five_hour_raw) if five_hour_raw else None,
        UsageWindow(**seven_day_raw) if seven_day_raw else None,
    )


def _endpoint() -> UsageEndpoint:
    """Read the credentials and say where, and how, to ask for usage."""
    credentials = _read_credentials()
    return UsageEndpoint(
        url=_USAGE_URL,
        headers={
            "Authorization": f"Bearer {credentials.access_token}",
            "anthropic-beta": _OAUTH_BETA,
        },
        expires_at=credentials.expires_at,
        parse=_usage_windows,
    )


def fetch() -> ProviderUsageData:
    """Return current Claude usage, encoding every failure as data."""
    return fetch_usage(_endpoint, named=PROVIDER_NAME)
