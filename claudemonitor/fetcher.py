"""Read Claude usage from the Anthropic OAuth endpoint.

Only what is Claude's own is here: where the credentials file is, what it
looks like, which URL to ask, and how to read the body that comes back — and,
for the session nudge, what to ask Claude Code and how to read its answer.
Everything else — the request, the timeout, and every way it can fail — is in
``usage_request``, which both providers share.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .cli_refresher import (
    DEFAULT_NUDGE_EFFORT,
    NUDGE_INSTRUCTIONS,
    last_output_line,
    parsed_object,
    token_count,
)
from .models import CliReply, ProviderUsageData, UsageWindow
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


def default_model() -> str:
    """Name the model Claude Code uses when the nudge names none, or "".

    The nudge runs with ``--setting-sources=``, so the model in
    ~/.claude/settings.json is not used. ANTHROPIC_MODEL still is. Without it,
    Claude Code picks the model itself and there is nothing to read.
    """
    return os.environ.get("ANTHROPIC_MODEL", "").strip()


def cli_arguments(model: str, effort: str) -> tuple[str, ...]:
    """Ask Claude Code for one reply, printed as a single JSON result object.

    Everything a one-word reply does not need is left out of the request: the
    tools, the MCP servers, the skills, the CLAUDE.md files, and the long
    default system prompt. That cut a nudge from about 27,700 tokens to about
    600. ``--bare`` would drop the same things, but it never reads OAuth, so it
    could neither renew a token nor start a subscription window.

    An empty value is written as ``--flag=`` rather than as a separate empty
    argument, because PowerShell 5.1 drops an empty argument and the command
    is also offered to the user to copy. A blank model is left out, so Claude
    Code uses its own default; a blank effort asks for low.
    """
    return (
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools=",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources=",
        "--system-prompt",
        NUDGE_INSTRUCTIONS,
        *(("--model", model) if model else ()),
        "--effort",
        effort or DEFAULT_NUDGE_EFFORT,
        "hi",
    )


def read_cli_reply(returncode: int, stdout: str, stderr: str) -> CliReply:
    """Read the one JSON result that `claude -p --output-format json` prints.

    An API error arrives as that same object with ``is_error`` set and the
    reason in ``result``, whatever the exit code says.
    """
    result = parsed_object(stdout)
    if result is None:
        return _unreadable_reply(returncode, stderr)
    if result.get("is_error") or returncode != 0:
        reason = str(result.get("result") or "").strip()
        return CliReply(
            succeeded=False,
            detail=reason or _exit_detail(returncode, stderr),
        )
    return CliReply(
        succeeded=True,
        reply_text=str(result.get("result") or "").strip(),
        cost_usd=_cost(result.get("total_cost_usd")),
        **_token_counts(result.get("usage")),
    )


def _cost(value: object) -> float | None:
    """Read the reported cost in US dollars, ignoring anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _token_counts(usage: object) -> dict:
    """Count every input token, cached or not, and the output tokens."""
    if not isinstance(usage, dict):
        return {}
    parts = [
        token_count(usage, key)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ]
    known = [part for part in parts if part is not None]
    return {
        "input_tokens": sum(known) if known else None,
        "output_tokens": token_count(usage, "output_tokens"),
    }


def _unreadable_reply(returncode: int, stderr: str) -> CliReply:
    """Describe a run whose output was not the JSON result it should be."""
    if returncode != 0:
        return CliReply(succeeded=False, detail=_exit_detail(returncode, stderr))
    return CliReply(
        succeeded=False, detail="The Claude CLI printed a reply that could not be read."
    )


def _exit_detail(returncode: int, stderr: str) -> str:
    """Say why a run failed from its last error line, or else its exit code."""
    return last_output_line(stderr) or f"The Claude CLI exited with code {returncode}."
