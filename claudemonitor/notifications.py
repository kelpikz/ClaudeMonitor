from __future__ import annotations

from dataclasses import dataclass

from .models import AnthropicUsageData

DEFAULT_THRESHOLDS: tuple[int, ...] = (50, 30, 10)


@dataclass(frozen=True)
class UsageNotification:
    """A desktop notification ready to hand to the tray layer."""

    title: str
    message: str


def _remaining_percent(data: AnthropicUsageData) -> float | None:
    """Return the fresh 5h percentage remaining, or None when it is unavailable."""
    if data.fetch_error is not None or data.five_hour is None:
        return None
    return 100.0 - data.five_hour.utilization


def _crossed_thresholds(
    previous: float,
    current: float,
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
) -> list[int]:
    """Find thresholds crossed downward between the previous and current readings."""
    return [threshold for threshold in thresholds if previous > threshold >= current]


class ThresholdNotifier:
    """Tracks 5h remaining percentage and emits notifications on downward crossings."""

    def __init__(self, thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS) -> None:
        self._thresholds = thresholds
        self._previous_remaining: float | None = None

    def check(self, data: AnthropicUsageData) -> list[UsageNotification]:
        """Update state from fresh usage data and return any threshold notifications."""
        current = _remaining_percent(data)
        if current is None:
            return []

        if self._previous_remaining is None:
            self._previous_remaining = current
            return []

        crossed = _crossed_thresholds(self._previous_remaining, current, self._thresholds)
        self._previous_remaining = current
        rounded_remaining = f"{current:.0f}%"
        return [
            UsageNotification(
                title=f"Claude usage below {threshold}%",
                message=f"5h usage has {rounded_remaining} remaining.",
            )
            for threshold in crossed
        ]
