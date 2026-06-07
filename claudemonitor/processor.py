from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .models import AnthropicUsageData, DisplayState


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
    if data.fetch_error:
        return f"Error — last update {elapsed_str} ago"
    return f"Updated {elapsed_str} ago"


def process(data: AnthropicUsageData, now: datetime, config: Config) -> DisplayState:
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

    remaining = 100.0 - data.five_hour.utilization
    if remaining > config.thresholds.amber_below:
        color = "green"
    elif remaining >= config.thresholds.red_below:
        color = "amber"
    else:
        color = "red"

    five_reset = _format_time_left(data.five_hour.resets_at, now)
    five_pct = f"{remaining:.0f}%"

    lines = [
        "Claude usage",
        f"5h:   {five_pct} left · resets in {five_reset}",
    ]
    if data.seven_day is not None:
        week_remaining = 100.0 - data.seven_day.utilization
        week_reset = _format_time_left(data.seven_day.resets_at, now)
        lines.append(f"Week: {week_remaining:.0f}% left · resets in {week_reset}")

    lines.append(_updated_at_line(data.fetched_at))

    return DisplayState(
        icon_color=color,
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
    return "Internal error — see log"


def internal_error_state(now: datetime) -> DisplayState:
    return DisplayState(
        icon_color="grey",
        tooltip="Internal error — see log",
        menu_status_label=f"Error — {now.strftime('%H:%M')}",
    )
