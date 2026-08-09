from __future__ import annotations

import math
from datetime import datetime

from .config import Config
from .models import AnthropicUsageData, DisplayState, UsageWindow

# Every taskbar string lives here so the label, the tooltip, and the menu can
# never drift apart. The companion shows this until the first fetch completes.
# The native label draws the Claude glyph itself, so these strings no longer
# spell out "Claude" — see win32_taskbar_window._draw_icon.
LOADING_TASKBAR_TEXT = "loading..."

_TASKBAR_ERROR_TEXTS = {
    "token_expired": "token expired",
    "timeout": "offline",
    "offline": "offline",
    "no_credentials": "not logged in",
    "bad_response": "bad response",
    "rate_limited": "rate limited",
}
_TASKBAR_UNKNOWN_ERROR_TEXT = "unavailable"
_TASKBAR_SIGN_IN_TEXT = "sign in"
_TASKBAR_NO_DATA_TEXT = "no data"
_TASKBAR_INTERNAL_ERROR_TEXT = "error"


def _format_time_left(resets_at: datetime | None, now: datetime) -> str:
    if resets_at is None:
        return "unknown"
    remaining = max(0, int((resets_at - now).total_seconds()))
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _taskbar_reset_text(resets_at: datetime | None, now: datetime) -> str:
    """Describe how long until a usage window resets, as hours and minutes."""
    if resets_at is None:
        # Matches the tooltip's "resets in unknown" for the same missing value.
        return "unknown"
    seconds = max(0, int((resets_at - now).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    # A "0m" countdown reads as broken rather than nearly finished.
    return "under a minute"


def _taskbar_text(window: UsageWindow, now: datetime) -> str:
    """Format remaining five-hour usage and its reset countdown compactly."""
    if _window_not_started(window):
        # Same rule the tooltip uses, so the two surfaces cannot disagree.
        return "100% (not started)"

    # Floor rather than round, so "100%" only ever means a truly untouched window.
    remaining_usage = math.floor(max(0.0, min(100.0, 100.0 - window.utilization)))
    return f"{remaining_usage}% ({_taskbar_reset_text(window.resets_at, now)})"


def _taskbar_error_text(error: str | None, session_refresh_exhausted: bool = False) -> str:
    """Map a fetch error to a short label that still says what went wrong."""
    if _needs_sign_in(error, session_refresh_exhausted):
        return _TASKBAR_SIGN_IN_TEXT
    return _TASKBAR_ERROR_TEXTS.get(error or "", _TASKBAR_UNKNOWN_ERROR_TEXT)


def _needs_sign_in(error: str | None, session_refresh_exhausted: bool) -> bool:
    """Return whether an expired token has outlived every automatic refresh.

    Only ``token_expired`` changes wording here. The nudge can also exhaust itself
    on a missing CLI while fetches succeed, and no other error is something a
    sign-in would fix.
    """
    return error == "token_expired" and session_refresh_exhausted


def _updated_at_line(fetched_at: datetime, now: datetime) -> str:
    """Return the time elapsed since the most recent fetch in whole seconds."""
    elapsed = max(0, int((now - fetched_at).total_seconds()))
    return f"Updated ({elapsed} seconds ago)"


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def _menu_label(
    data: AnthropicUsageData,
    now: datetime,
    session_refresh_exhausted: bool = False,
) -> str:
    elapsed = int((now - data.fetched_at).total_seconds())
    elapsed_str = _format_elapsed(max(0, elapsed))
    if data.fetch_error in ("timeout", "offline"):
        return f"Offline — last update {elapsed_str} ago"
    if _needs_sign_in(data.fetch_error, session_refresh_exhausted):
        return f"Sign-in needed — last update {elapsed_str} ago"
    if data.fetch_error == "token_expired":
        return f"Token expired — last update {elapsed_str} ago"
    if data.fetch_error == "no_credentials":
        return f"Not logged in — last update {elapsed_str} ago"
    if data.fetch_error == "rate_limited":
        return f"Rate limited — last update {elapsed_str} ago"
    if data.fetch_error:
        return f"Error — last update {elapsed_str} ago"
    return f"Updated {elapsed_str} ago"


def _icon_color(utilization: float, config: Config) -> str:
    """Map a 5h-window utilization (0–100 used %) to an icon color using the
    configured amber/red thresholds on the *remaining* percentage."""
    remaining = 100.0 - utilization
    if remaining > config.thresholds.amber_below:
        return "green"
    if remaining >= config.thresholds.red_below:
        return "amber"
    return "red"


def _window_not_started(window: UsageWindow) -> bool:
    """Return whether an API usage window has not started its first session."""
    return window.utilization == 0.0 and window.resets_at is None


def _usage_lines(data: AnthropicUsageData, now: datetime) -> list[str]:
    """Build the 'Claude usage' header plus the 5h (and optional weekly) "% left
    · resets in ..." lines. The caller appends a trailing status line."""
    if data.seven_day is not None and _window_not_started(data.seven_day):
        # A weekly session cannot be unstarted while the 5h session is active,
        # so showing both prompts would be redundant.
        return ["Claude usage", "Week: send a message to start the session"]

    if _window_not_started(data.five_hour):
        # No countdown to show yet — explain that it begins on the first message
        # rather than surfacing a misleading "100% left · resets in unknown".
        five_hour_line = "5h: send a message to start the session"
    else:
        five_remaining = 100.0 - data.five_hour.utilization
        five_reset = _format_time_left(data.five_hour.resets_at, now)
        five_hour_line = f"5h:   {five_remaining:.0f}% left · resets in {five_reset}"
    lines = [
        "Claude usage",
        five_hour_line,
    ]
    if data.seven_day is not None:
        if _window_not_started(data.seven_day):
            lines.append("Week: send a message to start the session")
        else:
            week_remaining = 100.0 - data.seven_day.utilization
            week_reset = _format_time_left(data.seven_day.resets_at, now)
            lines.append(f"Week: {week_remaining:.0f}% left · resets in {week_reset}")
    return lines


def _stale_state(last_good: AnthropicUsageData, now: datetime, config: Config) -> DisplayState:
    """Render the last successful usage data, flagged as stale because the most
    recent fetch was rate-limited (HTTP 429). Reset times stay accurate (they are
    absolute timestamps); only the freshness note reflects the older fetch."""
    color = _icon_color(last_good.five_hour.utilization, config)
    lines = _usage_lines(last_good, now)
    elapsed = _format_elapsed(max(0, int((now - last_good.fetched_at).total_seconds())))
    lines.append(f"Unable to fetch recent data ({elapsed} ago)")
    return DisplayState(
        icon_color=color,
        tooltip="\n".join(lines),
        menu_status_label=f"Rate limited — last update {elapsed} ago",
        taskbar_text=_taskbar_text(last_good.five_hour, now),
    )


def process(
    data: AnthropicUsageData,
    now: datetime,
    config: Config,
    last_good: AnthropicUsageData | None = None,
    session_refresh_exhausted: bool = False,
) -> DisplayState:
    # A rate-limit doesn't mean our data is wrong, just unrefreshed. If we have a
    # previous successful result, show it (flagged stale) instead of going grey.
    if (
        data.fetch_error == "rate_limited"
        and last_good is not None
        and last_good.five_hour is not None
    ):
        return _stale_state(last_good, now, config)

    label = _menu_label(data, now, session_refresh_exhausted)

    if data.fetch_error:
        tooltip = _error_tooltip(data.fetch_error, data, now, session_refresh_exhausted)
        tooltip += f"\n{_updated_at_line(data.fetched_at, now)}"
        return DisplayState(
            icon_color="grey",
            tooltip=tooltip,
            menu_status_label=label,
            taskbar_text=_taskbar_error_text(data.fetch_error, session_refresh_exhausted),
        )

    if data.five_hour is None:
        return DisplayState(
            icon_color="grey",
            tooltip=f"Claude usage\nNo usage data available\n{_updated_at_line(data.fetched_at, now)}",
            menu_status_label=label,
            taskbar_text=_TASKBAR_NO_DATA_TEXT,
        )

    lines = _usage_lines(data, now)
    lines.append(_updated_at_line(data.fetched_at, now))

    return DisplayState(
        icon_color=_icon_color(data.five_hour.utilization, config),
        tooltip="\n".join(lines),
        menu_status_label=label,
        taskbar_text=_taskbar_text(data.five_hour, now),
    )


def _error_tooltip(
    error: str,
    data: AnthropicUsageData,
    now: datetime,
    session_refresh_exhausted: bool = False,
) -> str:
    if _needs_sign_in(error, session_refresh_exhausted):
        # "Start Claude Code to refresh" is advice that cannot work once the
        # automatic refresh has already tried and failed.
        return "Claude sign-in needed — run: claude /login"
    if error == "token_expired":
        return "Claude token expired — start Claude Code to refresh"
    if error in ("timeout", "offline"):
        elapsed = int((now - data.fetched_at).total_seconds())
        return f"Offline — last update {_format_elapsed(max(0, elapsed))} ago"
    if error == "no_credentials":
        return "Claude credentials not found — log in via Claude Code"
    if error == "bad_response":
        return "Unexpected API response — see log for details"
    if error == "rate_limited":
        return "Rate limited — too many requests, will retry"
    return "Internal error — see log"


def internal_error_state(now: datetime) -> DisplayState:
    return DisplayState(
        icon_color="grey",
        tooltip="Internal error — see log",
        menu_status_label=f"Error — {now.strftime('%H:%M')}",
        taskbar_text=_TASKBAR_INTERNAL_ERROR_TEXT,
    )
