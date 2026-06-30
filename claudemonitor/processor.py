from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .models import AnthropicUsageData, DisplayState, UsageWindow


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


def _updated_at_line(fetched_at: datetime) -> str:
    local_time = fetched_at.astimezone().strftime("%H:%M:%S")
    return f"Updated at {local_time}"


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def _menu_label(data: AnthropicUsageData, now: datetime) -> str:
    elapsed = int((now - data.fetched_at).total_seconds())
    elapsed_str = _format_elapsed(max(0, elapsed))
    if data.fetch_error in ("timeout", "offline"):
        return f"Offline — last update {elapsed_str} ago"
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


def _five_hour_not_started(window: UsageWindow) -> bool:
    """The 5h rolling window only begins counting once the user sends their
    first message. Until then the API reports 0% utilization with no reset
    timestamp — distinct from an active window that simply has usage to spare."""
    return window.utilization == 0.0 and window.resets_at is None


def _usage_lines(data: AnthropicUsageData, now: datetime) -> list[str]:
    """Build the 'Claude usage' header plus the 5h (and optional weekly) "% left
    · resets in ..." lines. The caller appends a trailing status line."""
    if _five_hour_not_started(data.five_hour):
        # No countdown to show yet — explain that it begins on the first message
        # rather than surfacing a misleading "100% left · resets in unknown".
        five_hour_line = "5h:   not started — send a message to begin"
    else:
        five_remaining = 100.0 - data.five_hour.utilization
        five_reset = _format_time_left(data.five_hour.resets_at, now)
        five_hour_line = f"5h:   {five_remaining:.0f}% left · resets in {five_reset}"
    lines = [
        "Claude usage",
        five_hour_line,
    ]
    if data.seven_day is not None:
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
    )


def process(
    data: AnthropicUsageData,
    now: datetime,
    config: Config,
    last_good: AnthropicUsageData | None = None,
) -> DisplayState:
    # A rate-limit doesn't mean our data is wrong, just unrefreshed. If we have a
    # previous successful result, show it (flagged stale) instead of going grey.
    if (
        data.fetch_error == "rate_limited"
        and last_good is not None
        and last_good.five_hour is not None
    ):
        return _stale_state(last_good, now, config)

    label = _menu_label(data, now)

    if data.fetch_error:
        tooltip = _error_tooltip(data.fetch_error, data, now)
        tooltip += f"\n{_updated_at_line(data.fetched_at)}"
        return DisplayState(icon_color="grey", tooltip=tooltip, menu_status_label=label)

    if data.five_hour is None:
        return DisplayState(
            icon_color="grey",
            tooltip=f"Claude usage\nNo usage data available\n{_updated_at_line(data.fetched_at)}",
            menu_status_label=label,
        )

    lines = _usage_lines(data, now)
    lines.append(_updated_at_line(data.fetched_at))

    return DisplayState(
        icon_color=_icon_color(data.five_hour.utilization, config),
        tooltip="\n".join(lines),
        menu_status_label=label,
    )


def _error_tooltip(error: str, data: AnthropicUsageData, now: datetime) -> str:
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
    )
