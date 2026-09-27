from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from pydantic import BaseModel

from .config import CLAUDE_SETTINGS, CODEX_SETTINGS, ProviderSettings


@dataclass(frozen=True)
class Rect:
    """Represent the four screen coordinates around a rectangular area.

    Screen geometry crosses every layer — the Windows adapter measures it, the
    companion controller reasons about it, and tests assert on it — so it lives
    here with the other shared contracts rather than inside one of them.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Return the rectangle width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Return the rectangle height in pixels."""
        return self.bottom - self.top


@dataclass(frozen=True)
class Insets:
    """The padding one thing adds around another, edge by edge.

    A tab control reports how much of itself surrounds the page it shows, and
    the settings layout has to know that before it can place anything.
    """

    left: int
    top: int
    right: int
    bottom: int


def _claude_usage() -> "ProviderUsageData":
    """Fetch Claude usage.

    The import is deferred because ``fetcher`` imports this module for the
    types it returns. Reaching for it only when a fetch is actually asked for
    is what lets a provider carry its own fetcher rather than leaving the
    application to keep a table of which one answers for which.
    """
    from . import fetcher

    return fetcher.fetch()


def _codex_usage() -> "ProviderUsageData":
    """Fetch Codex usage; deferred for the same reason as Claude's."""
    from . import codex_fetcher

    return codex_fetcher.fetch()


def _claude_cli_arguments(model: str, effort: str) -> tuple[str, ...]:
    """Build Claude's nudge argv; deferred for the same reason as its fetch."""
    from . import fetcher

    return fetcher.cli_arguments(model, effort)


def _claude_cli_reply(returncode: int, stdout: str, stderr: str) -> "CliReply":
    """Read what Claude's CLI printed; deferred for the same reason."""
    from . import fetcher

    return fetcher.read_cli_reply(returncode, stdout, stderr)


def _codex_cli_arguments(model: str, effort: str) -> tuple[str, ...]:
    """Build Codex's nudge argv; deferred for the same reason."""
    from . import codex_fetcher

    return codex_fetcher.cli_arguments(model, effort)


def _codex_cli_reply(returncode: int, stdout: str, stderr: str) -> "CliReply":
    """Read what Codex's CLI printed; deferred for the same reason."""
    from . import codex_fetcher

    return codex_fetcher.read_cli_reply(returncode, stdout, stderr)


@dataclass(frozen=True)
class CliReply:
    """What one run of a provider's CLI answered.

    ``detail`` is one sentence saying why a run failed, in the CLI's own words
    where it gave any. ``reply_text`` is what the model answered. The counts,
    the cost, and the duration are ``None`` when nobody could say: Codex never
    reports a cost, and a CLI that never started has no duration.
    """

    succeeded: bool
    detail: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    reply_text: str = ""
    cost_usd: float | None = None
    duration_seconds: float | None = None


# The reasoning efforts both CLIs accept. The empty one asks for low, because a
# one-word reply needs no reasoning and the user's own default may be xhigh.
EFFORT_LEVELS: tuple[str, ...] = ("", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Provider:
    """One usage source the app tracks, and everything that varies with it.

    Adding a provider is adding one of these, the fetcher it names, a wording
    entry in ``processor``, and a glyph in ``icon_art``. Nothing else branches
    on which provider it is holding — every wrapper function that used to say
    "claude" or "codex" in its own name reads one of these fields instead.

    ``settings`` names the provider's own config section: whether it is
    tracked, and how its CLI is nudged. Every user-facing *sentence* about a
    provider is still written by ``processor.py``, which owns all display
    strings.
    """

    key: Literal["claude", "codex"]
    label: str
    # Where the settings window's "… usage online" button goes.
    usage_url: str
    # The CLI that can renew this provider's token, the argv of the cheapest
    # prompt that forces it to make a real request (given a model and an
    # effort), and how to read what it prints back.
    cli_executable: str
    cli_arguments: Callable[[str, str], tuple[str, ...]]
    read_cli_reply: Callable[[int, str, str], CliReply]
    fetch: Callable[[], "ProviderUsageData"]
    settings: ProviderSettings
    effort_levels: tuple[str, ...] = EFFORT_LEVELS


CLAUDE = Provider(
    key="claude",
    label="Claude",
    usage_url="https://console.anthropic.com/settings/usage",
    cli_executable="claude",
    cli_arguments=_claude_cli_arguments,
    read_cli_reply=_claude_cli_reply,
    fetch=_claude_usage,
    settings=CLAUDE_SETTINGS,
)

CODEX = Provider(
    key="codex",
    label="Codex",
    usage_url="https://chatgpt.com/codex/settings/usage",
    cli_executable="codex",
    cli_arguments=_codex_cli_arguments,
    read_cli_reply=_codex_cli_reply,
    fetch=_codex_usage,
    settings=CODEX_SETTINGS,
)

# Every provider the application knows, in the order they are displayed.
PROVIDERS: tuple[Provider, ...] = (CLAUDE, CODEX)


@dataclass(frozen=True)
class LabelSegment:
    """One provider's glyph-and-text pair inside the single taskbar label.

    The taskbar shows every tracked provider on one line, so the label is a
    list of these rather than a single string: the native window draws the
    provider's own mark before each segment's text.
    """

    provider: Provider
    text: str


# Every way a fetch can fail to produce usage, and the whole of the vocabulary
# the display layer has to answer for. It is a closed set rather than free text
# because three tables in ``processor`` are keyed by it: while any string was
# allowed, a value one table had and another did not was written twice before
# anybody noticed. ``unknown`` is what an unforeseen exception becomes — the
# repr goes to the log, where it can be read, rather than into the tray.
FetchError = Literal[
    "no_credentials",
    "token_expired",
    "rate_limited",
    "timeout",
    "offline",
    "bad_response",
    "no_data",
    "unknown",
]


class UsageWindow(BaseModel):
    utilization: float
    resets_at: datetime | None


class ProviderUsageData(BaseModel):
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    fetch_error: FetchError | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None
    fetched_at: datetime


@dataclass(frozen=True)
class DisplayState:
    """One provider's numbers, written for every surface that shows them.

    It carries the provider itself rather than its key, so nothing downstream
    has to look one up or guard against a key it cannot receive.
    """

    provider: Provider
    icon_color: Literal["green", "amber", "red", "grey"]
    tooltip: str
    menu_status_label: str
    taskbar_text: str
    # One line for the shared tray tooltip, which stacks a line per provider
    # inside a 127-character Windows buffer. The full ``tooltip`` above is far
    # too long for two providers to fit; this is the condensed form.
    tray_text: str


@dataclass(frozen=True)
class TrayState:
    """What the one tray icon shows for every provider at once.

    There is a single icon, so its colour is the worst of the providers', its
    hover text names each of them, and its menu opens with one status line per
    provider rather than the one line a per-provider icon used to carry.
    """

    icon_color: Literal["green", "amber", "red", "grey"]
    tooltip: str
    status_lines: list[str]


@dataclass(frozen=True)
class TaskbarLabel:
    """The whole taskbar label: one segment per provider, plus its hover text."""

    segments: list[LabelSegment]
    tooltip: str
