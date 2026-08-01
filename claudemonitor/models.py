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


class UsageWindow(BaseModel):
    utilization: float
    resets_at: datetime | None


class AnthropicUsageData(BaseModel):
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    fetch_error: str | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None
    fetched_at: datetime


class DisplayState(BaseModel):
    icon_color: Literal["green", "amber", "red", "grey"]
    tooltip: str
    menu_status_label: str
    taskbar_text: str
