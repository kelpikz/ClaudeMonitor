from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claudemonitor.models import AnthropicUsageData, UsageWindow
from claudemonitor.notifications import (
    ThresholdNotifier,
    UsageNotification,
    _crossed_thresholds,
    _remaining_percent,
)

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_data(
    *,
    utilization: float,
    fetch_error: str | None = None,
) -> AnthropicUsageData:
    """Build one successful usage response with a configurable 5h utilization."""
    return AnthropicUsageData(
        five_hour=UsageWindow(
            utilization=utilization,
            resets_at=NOW + timedelta(hours=2),
        ),
        fetch_error=fetch_error,
        fetched_at=NOW,
    )


class TestThresholdNotifier:
    """End-to-end crossing behavior from fetched usage data to notification text."""

    def test_first_observation_does_not_notify(self):
        notifier = ThresholdNotifier()
        assert notifier.check(make_data(utilization=60.0)) == []

    def test_notifies_when_remaining_crosses_50_percent(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=40.0))  # 60% remaining

        assert notifier.check(make_data(utilization=51.0)) == [
            UsageNotification(
                title="Claude usage below 50%",
                message="5h usage has 49% remaining.",
            )
        ]

    def test_notifies_when_remaining_crosses_30_percent(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=60.0))  # 40% remaining

        assert notifier.check(make_data(utilization=71.0)) == [
            UsageNotification(
                title="Claude usage below 30%",
                message="5h usage has 29% remaining.",
            )
        ]

    def test_notifies_when_remaining_crosses_10_percent(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=85.0))  # 15% remaining

        assert notifier.check(make_data(utilization=91.0)) == [
            UsageNotification(
                title="Claude usage below 10%",
                message="5h usage has 9% remaining.",
            )
        ]

    def test_large_drop_reports_each_crossed_threshold_in_order(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=20.0))  # 80% remaining

        assert notifier.check(make_data(utilization=95.0)) == [
            UsageNotification("Claude usage below 50%", "5h usage has 5% remaining."),
            UsageNotification("Claude usage below 30%", "5h usage has 5% remaining."),
            UsageNotification("Claude usage below 10%", "5h usage has 5% remaining."),
        ]

    def test_rearms_after_remaining_moves_back_above_threshold(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=40.0))  # 60% remaining
        notifier.check(make_data(utilization=55.0))  # 45% remaining, crosses 50
        notifier.check(make_data(utilization=30.0))  # 70% remaining after reset

        assert notifier.check(make_data(utilization=51.0)) == [
            UsageNotification(
                title="Claude usage below 50%",
                message="5h usage has 49% remaining.",
            )
        ]

    def test_fetch_errors_do_not_notify_or_update_previous_remaining(self):
        notifier = ThresholdNotifier()
        notifier.check(make_data(utilization=40.0))  # 60% remaining
        assert notifier.check(make_data(utilization=90.0, fetch_error="timeout")) == []

        assert notifier.check(make_data(utilization=51.0)) == [
            UsageNotification(
                title="Claude usage below 50%",
                message="5h usage has 49% remaining.",
            )
        ]

    def test_missing_five_hour_does_not_notify(self):
        notifier = ThresholdNotifier()
        data = AnthropicUsageData(five_hour=None, fetched_at=NOW)
        assert notifier.check(data) == []


class TestRemainingPercent:
    """_remaining_percent: converts API utilization to the remaining percentage."""

    def test_subtracts_utilization_from_100(self):
        data = make_data(utilization=33.6)
        assert _remaining_percent(data) == 66.4

    def test_returns_none_without_five_hour_window(self):
        data = AnthropicUsageData(five_hour=None, fetched_at=NOW)
        assert _remaining_percent(data) is None

    def test_returns_none_for_fetch_errors(self):
        data = make_data(utilization=90.0, fetch_error="offline")
        assert _remaining_percent(data) is None


class TestCrossedThresholds:
    """_crossed_thresholds: detects downward crossings over configured limits."""

    def test_crossing_requires_previous_value_above_threshold(self):
        assert _crossed_thresholds(previous=50.0, current=49.0) == []

    def test_current_value_equal_to_threshold_counts_as_crossed(self):
        assert _crossed_thresholds(previous=51.0, current=50.0) == [50]

    def test_no_crossing_when_usage_increases_remaining(self):
        assert _crossed_thresholds(previous=20.0, current=80.0) == []
