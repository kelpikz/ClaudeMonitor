from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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
