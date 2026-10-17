from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .cli_refresher import ManualRunState
from .config import Config
from .models import (
    CliReply,
    DisplayState,
    FetchError,
    LabelSegment,
    Provider,
    ProviderUsageData,
    TaskbarLabel,
    TrayState,
    UsageWindow,
)

# Every taskbar string lives here so the label, the tooltip, and the menu can
# never drift apart. The companion shows this until the first fetch completes.
# The native label draws the Claude glyph itself, so these strings no longer
# spell out "Claude" — see win32_taskbar_window._draw_icon.
LOADING_TASKBAR_TEXT = "loading..."
LOADING_TOOLTIP = "Claude Monitor — loading…"
NO_PROVIDER_TOOLTIP = "Claude Monitor — no provider tracked. Turn one on in Settings."
NO_PROVIDER_MENU_STATUS = "No provider tracked"

# Ascending order of how much the user needs to look at the tray icon. One icon
# now serves every provider, so the most severe colour is the one it takes:
# grey outranks green because "we do not know" must not read as "all is well",
# and both amber and red outrank grey because a real number beats a blank.
_ICON_COLOR_SEVERITY = ("green", "grey", "amber", "red")

_TASKBAR_INTERNAL_ERROR_TEXT = "error"


@dataclass(frozen=True)
class _Wording:
    """The provider-specific sentences the tooltip needs.

    Every other string in this module is provider-neutral, so this is the whole
    of what changes between Claude and Codex.
    """

    heading: str
    token_expired: str
    missing_credentials: str
    sign_in_needed: str


_WORDING: dict[str, _Wording] = {
    "claude": _Wording(
        heading="Claude usage",
        token_expired="Claude token expired — start Claude Code to refresh",
        missing_credentials="Claude credentials not found — log in via Claude Code",
        sign_in_needed="Claude sign-in needed — run: claude /login",
    ),
    "codex": _Wording(
        heading="Codex usage",
        token_expired="Codex token expired — start Codex to refresh",
        missing_credentials="Codex credentials not found — log in via Codex",
        sign_in_needed="Codex sign-in needed — run: codex login",
    ),
}


def _wording(provider: Provider) -> _Wording:
    """Return the sentences written about one provider."""
    return _WORDING[provider.key]


@dataclass(frozen=True)
class _ErrorDisplay:
    """The three things one fetch error is shown as, in one row.

    These used to be three tables written in the same order — a short label, a
    menu prefix, and a tooltip if-chain — and a value present in two of them
    but missing from the third fell through to wording that fitted a different
    failure. One row per error makes that impossible: a new ``FetchError``
    has nowhere to hide until it has all three.

    ``tooltip`` is a callable because two of the sentences are not fixed: one
    names the provider, another counts how long the data has been stale.
    """

    taskbar_text: str
    menu_prefix: str
    tooltip: Callable[[_Wording, str], str]


_OFFLINE_DISPLAY = _ErrorDisplay(
    taskbar_text="offline",
    menu_prefix="Offline",
    tooltip=lambda wording, elapsed: f"Offline — last update {elapsed} ago",
)

_ERROR_DISPLAY: dict[FetchError, _ErrorDisplay] = {
    "no_credentials": _ErrorDisplay(
        taskbar_text="not logged in",
        menu_prefix="Not logged in",
        tooltip=lambda wording, elapsed: wording.missing_credentials,
    ),
    "token_expired": _ErrorDisplay(
        taskbar_text="token expired",
        menu_prefix="Token expired",
        tooltip=lambda wording, elapsed: wording.token_expired,
    ),
    "rate_limited": _ErrorDisplay(
        taskbar_text="rate limited",
        menu_prefix="Rate limited",
        tooltip=lambda wording, elapsed: "Rate limited — too many requests, will retry",
    ),
    # A timeout is a network failure the user can do nothing about, so it is
    # told as the same story as an unreachable host rather than as its own.
    "timeout": _OFFLINE_DISPLAY,
    "offline": _OFFLINE_DISPLAY,
    "bad_response": _ErrorDisplay(
        taskbar_text="bad response",
        menu_prefix="Error",
        tooltip=lambda wording, elapsed: "Unexpected API response — see log for details",
    ),
    "no_data": _ErrorDisplay(
        taskbar_text="no data",
        menu_prefix="Error",
        tooltip=lambda wording, elapsed: "No usage data yet",
    ),
    "unknown": _ErrorDisplay(
        taskbar_text="unavailable",
        menu_prefix="Error",
        tooltip=lambda wording, elapsed: "Internal error — see log",
    ),
}

# What an expired token becomes once every automatic refresh has been spent:
# "start Claude Code to refresh" is advice that cannot work any more.
_SIGN_IN_DISPLAY = _ErrorDisplay(
    taskbar_text="sign in",
    menu_prefix="Sign-in needed",
    tooltip=lambda wording, elapsed: wording.sign_in_needed,
)


def _display_for(
    error: FetchError,
    session_refresh_exhausted: bool = False,
) -> _ErrorDisplay:
    """Return how one fetch error is shown, on every surface at once."""
    if _needs_sign_in(error, session_refresh_exhausted):
        return _SIGN_IN_DISPLAY
    return _ERROR_DISPLAY[error]


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


def _tray_text(five_hour_text: str, seven_day: UsageWindow | None) -> str:
    """Condense one provider onto the single line the tray tooltip stacks.

    The five-hour reading is what the taskbar already shows; the weekly number
    is appended because the tray icon is the only place left that reports it.
    """
    if seven_day is None or _window_not_started(seven_day):
        return five_hour_text
    week_remaining = 100.0 - seven_day.utilization
    return f"{five_hour_text} · week {week_remaining:.0f}%"


def _needs_sign_in(error: FetchError | None, session_refresh_exhausted: bool) -> bool:
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
    data: ProviderUsageData,
    now: datetime,
    session_refresh_exhausted: bool = False,
) -> str:
    elapsed = int((now - data.fetched_at).total_seconds())
    elapsed_str = _format_elapsed(max(0, elapsed))
    if data.fetch_error is None:
        return f"Updated {elapsed_str} ago"
    prefix = _display_for(data.fetch_error, session_refresh_exhausted).menu_prefix
    return f"{prefix} — last update {elapsed_str} ago"


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


def _usage_lines(
    data: ProviderUsageData,
    now: datetime,
    provider: Provider,
) -> list[str]:
    """Build the "<provider> usage" header plus the 5h (and optional weekly)
    "% left · resets in ..." lines. The caller appends a trailing status line."""
    heading = _wording(provider).heading
    if data.seven_day is not None and _window_not_started(data.seven_day):
        # A weekly session cannot be unstarted while the 5h session is active,
        # so showing both prompts would be redundant.
        return [heading, "Week: send a message to start the session"]

    if _window_not_started(data.five_hour):
        # No countdown to show yet — explain that it begins on the first message
        # rather than surfacing a misleading "100% left · resets in unknown".
        five_hour_line = "5h: send a message to start the session"
    else:
        five_remaining = 100.0 - data.five_hour.utilization
        five_reset = _format_time_left(data.five_hour.resets_at, now)
        five_hour_line = f"5h:   {five_remaining:.0f}% left · resets in {five_reset}"
    lines = [
        heading,
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


def _stale_state(
    last_good: ProviderUsageData,
    now: datetime,
    config: Config,
    provider: Provider,
) -> DisplayState:
    """Render the last successful usage data, flagged as stale because the most
    recent fetch was rate-limited (HTTP 429). Reset times stay accurate (they are
    absolute timestamps); only the freshness note reflects the older fetch."""
    color = _icon_color(last_good.five_hour.utilization, config)
    lines = _usage_lines(last_good, now, provider)
    elapsed = _format_elapsed(max(0, int((now - last_good.fetched_at).total_seconds())))
    lines.append(f"Unable to fetch recent data ({elapsed} ago)")
    taskbar_text = _taskbar_text(last_good.five_hour, now)
    return DisplayState(
        provider=provider,
        icon_color=color,
        tooltip="\n".join(lines),
        menu_status_label=f"Rate limited — last update {elapsed} ago",
        taskbar_text=taskbar_text,
        tray_text=_tray_text(taskbar_text, last_good.seven_day),
    )


def process(
    data: ProviderUsageData,
    now: datetime,
    config: Config,
    provider: Provider,
    *,
    last_good: ProviderUsageData | None = None,
    session_refresh_exhausted: bool = False,
) -> DisplayState:
    # A rate-limit doesn't mean our data is wrong, just unrefreshed. If we have a
    # previous successful result, show it (flagged stale) instead of going grey.
    if (
        data.fetch_error == "rate_limited"
        and last_good is not None
        and last_good.five_hour is not None
    ):
        return _stale_state(last_good, now, config, provider)

    label = _menu_label(data, now, session_refresh_exhausted)

    if data.fetch_error:
        tooltip = _error_tooltip(
            data.fetch_error,
            data,
            now,
            provider,
            session_refresh_exhausted=session_refresh_exhausted,
        )
        tooltip += f"\n{_updated_at_line(data.fetched_at, now)}"
        error_text = _display_for(
            data.fetch_error, session_refresh_exhausted
        ).taskbar_text
        return DisplayState(
            provider=provider,
            icon_color="grey",
            tooltip=tooltip,
            menu_status_label=label,
            taskbar_text=error_text,
            tray_text=error_text,
        )

    if data.five_hour is None:
        heading = _wording(provider).heading
        return DisplayState(
            provider=provider,
            icon_color="grey",
            tooltip=f"{heading}\nNo usage data available\n{_updated_at_line(data.fetched_at, now)}",
            menu_status_label=label,
            taskbar_text=_ERROR_DISPLAY["no_data"].taskbar_text,
            tray_text=_ERROR_DISPLAY["no_data"].taskbar_text,
        )

    lines = _usage_lines(data, now, provider)
    lines.append(_updated_at_line(data.fetched_at, now))

    taskbar_text = _taskbar_text(data.five_hour, now)
    return DisplayState(
        provider=provider,
        icon_color=_icon_color(data.five_hour.utilization, config),
        tooltip="\n".join(lines),
        menu_status_label=label,
        taskbar_text=taskbar_text,
        tray_text=_tray_text(taskbar_text, data.seven_day),
    )


def _error_tooltip(
    error: FetchError,
    data: ProviderUsageData,
    now: datetime,
    provider: Provider,
    *,
    session_refresh_exhausted: bool = False,
) -> str:
    """Return the first line of the tooltip for one failed fetch."""
    elapsed = _format_elapsed(max(0, int((now - data.fetched_at).total_seconds())))
    return _display_for(error, session_refresh_exhausted).tooltip(
        _wording(provider), elapsed
    )


def internal_error_state(now: datetime, provider: Provider) -> DisplayState:
    return DisplayState(
        provider=provider,
        icon_color="grey",
        tooltip="Internal error — see log",
        menu_status_label=f"Error — {now.strftime('%H:%M')}",
        taskbar_text=_TASKBAR_INTERNAL_ERROR_TEXT,
        tray_text=_TASKBAR_INTERNAL_ERROR_TEXT,
    )


def taskbar_label(states: list[DisplayState]) -> TaskbarLabel:
    """Combine each provider's display state into the one taskbar label.

    The label is a list of glyph-and-text segments rather than a single string,
    so the native window can draw each provider's own icon before its numbers.
    The tooltip stacks the full per-provider detail, separated by a blank line.
    """
    if not states:
        # Every provider is switched off. The companion hides an empty label.
        return TaskbarLabel(segments=[], tooltip=NO_PROVIDER_TOOLTIP)
    return TaskbarLabel(
        segments=[
            LabelSegment(provider=state.provider, text=state.taskbar_text)
            for state in states
        ],
        tooltip="\n\n".join(state.tooltip for state in states),
    )


def tray_status(states: list[DisplayState]) -> TrayState:
    """Condense every tracked provider into what the one tray icon shows.

    The icon has one colour for two providers, so it takes the more serious of
    the two; the tooltip and the menu name each provider, because a number with
    no name beside it belongs to nobody.
    """
    if not states:
        # The icon stays, because Settings is where a provider is turned on.
        return TrayState(
            icon_color="grey",
            tooltip=NO_PROVIDER_TOOLTIP,
            status_lines=[NO_PROVIDER_MENU_STATUS],
        )
    return TrayState(
        icon_color=max(states, key=_color_severity).icon_color,
        tooltip="\n".join(
            f"{state.provider.label}  {state.tray_text}"
            for state in states
        ),
        status_lines=[
            f"{state.provider.label} — {state.menu_status_label}"
            for state in states
        ],
    )


def _color_severity(state: DisplayState) -> int:
    """Rank one provider's colour so the worst of them can be picked."""
    return _ICON_COLOR_SEVERITY.index(state.icon_color)


def loading_label(providers: list[Provider]) -> TaskbarLabel:
    """Build the placeholder label shown before the first fetch returns."""
    return TaskbarLabel(
        segments=[
            LabelSegment(provider=provider, text=LOADING_TASKBAR_TEXT)
            for provider in providers
        ],
        tooltip=LOADING_TOOLTIP,
    )


# ------------------------------------------------------------- manual CLI run
# What the box under the settings window's Run now button says. It is written
# here with every other sentence the user reads.


def manual_run_text(provider: Provider, state: ManualRunState) -> str:
    """Describe the last copy or run of one provider's command, one fact a line."""
    if state.phase == "copied":
        return f"Command copied to the clipboard:\n{state.command}"
    if state.phase == "running":
        return f"Running…\n{state.command}"
    if state.phase == "finished" and state.reply is not None:
        return "\n".join([*_run_result_lines(provider, state.reply), "", "Command:", state.command])
    # Nothing to say yet: an empty text is what hides the Last run box.
    return ""


def _run_result_lines(provider: Provider, reply: CliReply) -> list[str]:
    """Say how a finished run went: its reply, tokens, and cost, or why it failed."""
    if not reply.succeeded:
        return [_outcome("Failed after", reply.duration_seconds), reply.detail]
    return [
        _outcome("Succeeded in", reply.duration_seconds),
        f"Reply: {_one_line(reply.reply_text) or '(empty)'}",
        f"Tokens: {_token_text(reply.input_tokens, reply.output_tokens)}",
        f"Cost: {_cost_text(provider, reply.cost_usd)}",
    ]


def _outcome(words: str, seconds: float | None) -> str:
    """Write "Succeeded in 6.3 s.", or only "Succeeded." when the time is unknown."""
    if seconds is None:
        return f"{words.split()[0]}."
    return f"{words} {seconds:.1f} s."


def _one_line(text: str) -> str:
    """Join a reply that spans several lines into one."""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _token_text(input_tokens: int | None, output_tokens: int | None) -> str:
    """Write the two token counts, naming whichever the CLI did not report."""
    if input_tokens is None and output_tokens is None:
        return "not reported"
    return f"{_count(input_tokens)} in, {_count(output_tokens)} out"


def _count(tokens: int | None) -> str:
    """Write one token count with thousands separators."""
    return "unknown" if tokens is None else f"{tokens:,}"


def _cost_text(provider: Provider, cost_usd: float | None) -> str:
    """Write the cost the CLI reported. Codex never reports one."""
    if cost_usd is None:
        return f"not reported by the {provider.label} CLI"
    return f"${cost_usd:.4f}"
