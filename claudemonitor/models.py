from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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


@dataclass(frozen=True)
class Provider:
    """Identify one usage source the app tracks.

    Only identity lives here. Every user-facing sentence about a provider is
    written by ``processor.py``, which owns all display strings.
    """

    key: Literal["claude", "codex"]
    label: str


CLAUDE = Provider(key="claude", label="Claude")
CODEX = Provider(key="codex", label="Codex")

# Every provider the application knows, by the key its display state carries.
PROVIDERS: dict[str, Provider] = {CLAUDE.key: CLAUDE, CODEX.key: CODEX}


@dataclass(frozen=True)
class LabelSegment:
    """One provider's glyph-and-text pair inside the single taskbar label.

    The taskbar shows every tracked provider on one line, so the label is a
    list of these rather than a single string: the native window draws the
    glyph named by ``provider_key`` before each segment's text.
    """

    provider_key: str
    text: str


class UsageWindow(BaseModel):
    utilization: float
    resets_at: datetime | None


class ProviderUsageData(BaseModel):
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    fetch_error: str | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None
    fetched_at: datetime


class DisplayState(BaseModel):
    provider_key: str
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
