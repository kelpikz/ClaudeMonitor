"""Read OpenAI Codex usage from the ChatGPT backend.

Only what is Codex's own is here: where the Codex CLI keeps its ``auth.json``,
how to read an expiry out of the JWT it holds, which URL to ask, and how to
read the body that comes back — and, for the session nudge, what to ask the
Codex CLI and how to read the events it prints. The request itself, and every way it can fail,
is in ``usage_request``, which both providers share.

This module only ever reads that credentials file — refreshing an expired
token is the CLI's job, and ``cli_refresher.py`` asks it to do so.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tomllib
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

_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_USER_AGENT = "ClaudeMonitor/0.1"

PROVIDER_NAME = "codex"


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str
    expires_at: datetime | None


def _codex_home() -> Path:
    """Locate the Codex CLI's own folder, honoring a CODEX_HOME override."""
    codex_home = os.environ.get("CODEX_HOME")
    return Path(codex_home) if codex_home else Path.home() / ".codex"


def _auth_path() -> Path:
    """Locate the Codex CLI's credential file."""
    return _codex_home() / "auth.json"


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


def cli_arguments(model: str, effort: str) -> tuple[str, ...]:
    """Ask the Codex CLI for one non-interactive turn, printed as JSON events.

    read-only keeps a stray model reply from editing real files, and the repo
    check would otherwise refuse to start from the tray app's directory.

    Every override after that removes something a one-word reply does not need:
    the long base instructions, AGENTS.md, the prompt sections, the tools, the
    plugins, and the user's MCP servers. That cut a nudge from about 21,100
    tokens to about 1,900. The user's own config is still read, so a blank
    model is the one it names; a blank effort asks for low, because that
    config may ask for far more reasoning than a one-word prompt needs.
    """
    return (
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        *(("-m", model) if model else ()),
        *_overrides(
            f"model_reasoning_effort={effort or DEFAULT_NUDGE_EFFORT}",
            f"instructions={NUDGE_INSTRUCTIONS}",
            *_LEAN_PROMPT_OVERRIDES,
            *(f"features.{feature}=false" for feature in _UNNEEDED_FEATURES),
            *(f"mcp_servers.{name}.enabled=false" for name in _configured_mcp_servers()),
        ),
        "hi",
    )


# Each value is written without quotes. Codex reads a value that is not valid
# TOML as a plain string, and PowerShell 5.1 strips the inner quotes of a
# command the user copies. `base_instructions` is not here because Codex
# ignores it; `instructions` is the key it reads.
_LEAN_PROMPT_OVERRIDES: tuple[str, ...] = (
    "project_doc_max_bytes=0",
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "include_collaboration_mode_instructions=false",
    "skills.include_instructions=false",
    "agents.enabled=false",
    "web_search=disabled",
    "notify=[]",
)

# Switched off with `-c features.<name>=false` rather than `--disable <name>`:
# --disable stops Codex on a name it does not know, so a feature removed by a
# Codex update would break every nudge. An unknown `-c` key is ignored.
_UNNEEDED_FEATURES: tuple[str, ...] = (
    "plugins",
    "apps",
    "hooks",
    "shell_tool",
    "unified_exec",
    "view_image",
    "multi_agent",
    "goals",
    "image_generation",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "sleep_tool",
    "tool_suggest",
    "skill_search",
    "code_mode_host",
    "workspace_dependencies",
)

# A name that is a TOML bare key, so it can sit in a dotted path unquoted.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _overrides(*settings: str) -> tuple[str, ...]:
    """Write each setting as the `-c key=value` pair Codex reads it from."""
    return tuple(part for setting in settings for part in ("-c", setting))


def default_model() -> str:
    """Name the model `codex exec` uses when the nudge names none, or "".

    That is the ``model`` in the user's config.toml, unless the profile it
    selects has a model of its own.
    """
    config = _read_config()
    model = config.get("model")
    profiles = config.get("profiles")
    profile_name = config.get("profile")
    if isinstance(profiles, dict) and isinstance(profile_name, str):
        profile = profiles.get(profile_name)
        if isinstance(profile, dict) and "model" in profile:
            model = profile["model"]
    return model if isinstance(model, str) else ""


def _configured_mcp_servers() -> tuple[str, ...]:
    """Name every MCP server the user's Codex config starts.

    `mcp_servers={}` does not remove them, so each one is switched off by name.
    A name that would need quotes is skipped, and an unreadable config names
    none: the nudge then costs a few hundred tokens more, but it still runs.
    """
    servers = _read_config().get("mcp_servers")
    if not isinstance(servers, dict):
        return ()
    return tuple(name for name in servers if _BARE_KEY.match(name))


def _read_config() -> dict:
    """Read the user's Codex config; a missing or broken file reads as empty."""
    try:
        return tomllib.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _config_path() -> Path:
    """Locate the Codex CLI's config file, honoring a CODEX_HOME override."""
    return _codex_home() / "config.toml"


def read_cli_reply(returncode: int, stdout: str, stderr: str) -> CliReply:
    """Read the JSON events `codex exec --json` prints, one per line.

    A completed turn is the only proof of a real request. An error event
    before it may be a retry notice the CLI recovered from, so errors are
    only read when no turn completed.
    """
    events = _events(stdout)
    completed = next(
        (event for event in reversed(events) if event.get("type") == "turn.completed"),
        None,
    )
    if returncode == 0 and completed is not None:
        usage = completed.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return CliReply(
            succeeded=True,
            input_tokens=token_count(usage, "input_tokens"),
            output_tokens=token_count(usage, "output_tokens"),
            reply_text=_reply_text(events),
        )
    return CliReply(succeeded=False, detail=_failure_detail(events, returncode, stderr))


def _reply_text(events: list[dict]) -> str:
    """Return what the model answered: the text of its last message.

    Other items complete too — a reasoning item, say — and carry no text.
    """
    for event in reversed(events):
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and item.get("text"):
                return str(item["text"]).strip()
    return ""


def _events(stdout: str) -> list[dict]:
    """Parse each line that is a JSON object, skipping any that is not."""
    parsed = (parsed_object(line) for line in stdout.splitlines())
    return [event for event in parsed if event is not None]


def _failure_detail(events: list[dict], returncode: int, stderr: str) -> str:
    """Say why a run failed: its last error event, its last error line, or its code."""
    for event in reversed(events):
        message = _event_message(event)
        if message:
            return _plain_message(message)
    if last_output_line(stderr):
        return last_output_line(stderr)
    if returncode != 0:
        return f"The Codex CLI exited with code {returncode}."
    return "The Codex CLI finished without a reply."


def _event_message(event: dict) -> str:
    """Return the message an error or failed-turn event carries, if any."""
    if event.get("type") == "turn.failed":
        error = event.get("error")
        return str(error.get("message") or "") if isinstance(error, dict) else ""
    if event.get("type") == "error":
        return str(event.get("message") or "")
    return ""


def _plain_message(message: str) -> str:
    """Unwrap an API error body that the CLI passed on as its message."""
    body = parsed_object(message)
    if body is None:
        return message.strip()
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return str(body.get("message") or message).strip()
