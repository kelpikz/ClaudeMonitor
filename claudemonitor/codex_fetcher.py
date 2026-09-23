"""Read OpenAI Codex usage from the ChatGPT backend.

Only what is Codex's own is here: where the Codex CLI keeps its ``auth.json``,
how to read an expiry out of the JWT it holds, which URL to ask, and how to
read the body that comes back. The request itself, and every way it can fail,
is in ``usage_request``, which both providers share.

This module only ever reads that credentials file — refreshing an expired
token is the CLI's job, and ``cli_refresher.py`` asks it to do so.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ProviderUsageData, UsageWindow
from .usage_request import UsageEndpoint, UsageWindows, fetch_usage

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_USER_AGENT = "ClaudeMonitor/0.1"

PROVIDER_NAME = "codex"


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


def _reset_time(reset_at: float | int | str | None) -> datetime | None:
    """Convert the window's Unix-seconds reset stamp into an aware datetime."""
    if reset_at is None:
        return None
    return datetime.fromtimestamp(float(reset_at), tz=timezone.utc)


def _usage_window(raw: dict | None) -> UsageWindow | None:
    """Map one Codex rate-limit window onto the shared UsageWindow model."""
    if not raw:
        return None
    return UsageWindow(
        utilization=float(raw["used_percent"]),
        resets_at=_reset_time(raw.get("reset_at")),
    )


def _usage_windows(body: dict) -> UsageWindows:
    """Map Codex's two rate-limit windows onto the shared model.

    Raises on a wrongly shaped window, which the shared request reports as
    ``bad_response`` rather than showing an invented number.
    """
    rate_limit = body.get("rate_limit") or {}
    return (
        _usage_window(rate_limit.get("primary_window")),
        _usage_window(rate_limit.get("secondary_window")),
    )


def _endpoint() -> UsageEndpoint:
    """Read the credentials and say where, and how, to ask for usage."""
    credentials = _read_credentials()
    return UsageEndpoint(
        url=_USAGE_URL,
        headers={
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-Id": credentials.account_id,
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
        expires_at=credentials.expires_at,
        parse=_usage_windows,
    )


def fetch() -> ProviderUsageData:
    """Return current Codex usage, encoding every failure as data."""
    return fetch_usage(_endpoint, named=PROVIDER_NAME)
