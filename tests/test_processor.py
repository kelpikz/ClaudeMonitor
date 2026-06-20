from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from claudemonitor.config import Config, ThresholdsConfig
from claudemonitor.models import AnthropicUsageData, UsageWindow
from claudemonitor.processor import (
    _error_tooltip,
    _format_elapsed,
    _format_time_left,
    _menu_label,
    _updated_at_line,
    internal_error_state,
    process,
)

# A fixed, timezone-aware "current time" shared by every test. Using a constant
# (rather than datetime.now()) keeps all elapsed/remaining calculations
# deterministic regardless of when or where the suite runs.
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_data(
    *,
    five_hour: UsageWindow | None = None,
    seven_day: UsageWindow | None = None,
    fetch_error: str | None = None,
    fetched_at: datetime = NOW,
) -> AnthropicUsageData:
    """Build an AnthropicUsageData with sensible defaults so each test only
    has to spell out the fields it actually cares about."""
    return AnthropicUsageData(
        five_hour=five_hour,
        seven_day=seven_day,
        fetch_error=fetch_error,
        fetched_at=fetched_at,
    )


# ===========================================================================
# process — the public entry point.
#
# These tests drive the module through its real surface. Because process()
# delegates all formatting to the private helpers, exercising every branch
# here also exercises _menu_label, _format_time_left, _format_elapsed,
# _error_tooltip and _updated_at_line in their natural context. The focused
# per-helper tests further down pin the fiddly edge cases of each helper in
# isolation so a failure points straight at the culprit.
# ===========================================================================


class TestProcessHappyPath:
    """The fully-populated success case: no error, both usage windows present.

    This single scenario fans out through the whole formatting stack, so we
    assert the complete DisplayState end to end:
        - icon_color  (the threshold logic in process itself)
        - tooltip      (assembled from _format_time_left + _updated_at_line)
        - menu label   (_menu_label -> _format_elapsed)
    """

    def _state(self):
        # 70% of the 5h window left -> well above the default amber_below=50,
        # so we expect green. Both windows have concrete reset times so the
        # "resets in ..." text comes from _format_time_left (not "unknown").
        data = make_data(
            five_hour=UsageWindow(
                utilization=30.0, resets_at=NOW + timedelta(hours=2, minutes=15)
            ),
            seven_day=UsageWindow(
                utilization=10.0, resets_at=NOW + timedelta(days=3, hours=4)
            ),
            fetched_at=NOW - timedelta(seconds=15),
        )
        return process(data, NOW, Config())

    def test_icon_is_green_with_plenty_remaining(self):
        # 70% remaining > amber_below (50) -> green branch of process().
        assert self._state().icon_color == "green"

    def test_tooltip_has_full_three_line_body_plus_timestamp(self):
        # Verifies the exact assembled tooltip: header, 5h line, week line,
        # and the trailing "Updated at" line. This is the one place we check
        # the entire multi-line layout in one shot.
        lines = self._state().tooltip.split("\n")
        assert lines[0] == "Claude usage"
        assert lines[1] == "5h:   70% left · resets in 2h 15m"
        assert lines[2] == "Week: 90% left · resets in 3d 4h"
        assert re.fullmatch(r"Updated at \d{2}:\d{2}:\d{2}", lines[3])

    def test_menu_label_reports_freshness(self):
        # On success the label is just how long ago we fetched, formatted by
        # _menu_label -> _format_elapsed (15s -> "15s").
        assert self._state().menu_status_label == "Updated 15s ago"


class TestProcessColors:
    """The color threshold logic lives in process() itself, so these cases
    are not redundant with any helper. Defaults: amber_below=50, red_below=20.
    Note the asymmetric comparisons in the source:
        remaining > amber_below      -> green
        remaining >= red_below       -> amber
        otherwise                    -> red
    The parametrization pins those exact boundaries."""

    @pytest.mark.parametrize(
        "utilization,expected",
        [
            (0.0, "green"),    # 100% remaining
            (49.0, "green"),   # 51% remaining, strictly > 50
            (50.0, "amber"),   # exactly 50% remaining: NOT > 50, so amber
            (50.1, "amber"),   # 49.9% remaining, comfortably in the amber band
            (80.0, "amber"),   # exactly 20% remaining == red_below, still amber
            (80.1, "red"),     # 19.9% remaining, drops below red_below -> red
            (100.0, "red"),    # 0% remaining
        ],
    )
    def test_color_by_remaining(self, utilization, expected):
        data = make_data(
            five_hour=UsageWindow(utilization=utilization, resets_at=NOW + timedelta(hours=1))
        )
        assert process(data, NOW, Config()).icon_color == expected

    def test_custom_thresholds_are_respected(self):
        # With amber_below=80/red_below=40, 30% remaining falls below 40 -> red,
        # proving process() reads the config rather than hardcoding 50/20.
        config = Config(thresholds=ThresholdsConfig(amber_below=80, red_below=40))
        data = make_data(
            five_hour=UsageWindow(utilization=70.0, resets_at=NOW + timedelta(hours=1))
        )
        assert process(data, NOW, config).icon_color == "red"


class TestProcessTooltipDetails:
    """Smaller tooltip behaviors that aren't covered by the happy path."""

    def test_week_line_omitted_when_no_seven_day_window(self):
        # When seven_day is None the tooltip should have no "Week:" line at all.
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2))
        )
        lines = process(data, NOW, Config()).tooltip.split("\n")
        assert not any(line.startswith("Week:") for line in lines)

    def test_remaining_percentage_is_rounded_to_whole_number(self):
        # 100 - 33.6 = 66.4, formatted with "{:.0f}" -> "66%".
        data = make_data(
            five_hour=UsageWindow(utilization=33.6, resets_at=NOW + timedelta(hours=1))
        )
        assert "66% left" in process(data, NOW, Config()).tooltip

    def test_unknown_reset_when_resets_at_is_none(self):
        # A window with no reset timestamp should surface "unknown" (from
        # _format_time_left) rather than crashing or showing a bogus duration.
        data = make_data(five_hour=UsageWindow(utilization=10.0, resets_at=None))
        assert "resets in unknown" in process(data, NOW, Config()).tooltip


class TestProcessNoData:
    """No fetch error, but the API returned no 5h window — a distinct grey
    state separate from the error states."""

    def test_missing_five_hour_is_grey_with_explanatory_tooltip(self):
        data = make_data(five_hour=None)
        state = process(data, NOW, Config())
        assert state.icon_color == "grey"
        assert "No usage data available" in state.tooltip

    def test_no_data_tooltip_still_ends_with_updated_line(self):
        data = make_data(five_hour=None)
        last_line = process(data, NOW, Config()).tooltip.split("\n")[-1]
        assert last_line.startswith("Updated at ")


class TestProcessErrors:
    """When fetch_error is set, process() short-circuits to a grey icon and an
    error tooltip before ever looking at the usage windows. These cases drive
    _error_tooltip and the error branches of _menu_label."""

    def test_error_yields_grey_icon(self):
        data = make_data(fetch_error="timeout", fetched_at=NOW - timedelta(minutes=1))
        assert process(data, NOW, Config()).icon_color == "grey"

    def test_error_tooltip_is_message_then_updated_line(self):
        # First line is the human-readable error, last line is the timestamp.
        data = make_data(fetch_error="token_expired", fetched_at=NOW)
        lines = process(data, NOW, Config()).tooltip.split("\n")
        assert lines[0] == "Claude token expired — start Claude Code to refresh"
        assert lines[-1].startswith("Updated at ")

    def test_error_sets_matching_menu_label(self):
        data = make_data(fetch_error="no_credentials", fetched_at=NOW - timedelta(minutes=2))
        assert process(data, NOW, Config()).menu_status_label == "Not logged in — last update 2m ago"

    def test_error_takes_precedence_over_present_usage_data(self):
        # Even with a perfectly good five_hour window, an error must win and
        # produce grey — proving the error check happens first.
        data = make_data(
            five_hour=UsageWindow(utilization=10.0, resets_at=NOW + timedelta(hours=1)),
            fetch_error="bad_response",
        )
        assert process(data, NOW, Config()).icon_color == "grey"


# ===========================================================================
# internal_error_state — the hard-coded fallback used when process() itself
# (or its caller) blows up. No inputs beyond `now`, so just two assertions.
# ===========================================================================


class TestInternalErrorState:
    def test_grey_icon_and_fixed_tooltip(self):
        state = internal_error_state(NOW)
        assert state.icon_color == "grey"
        assert state.tooltip == "Internal error — see log"

    def test_menu_label_is_error_with_hh_mm(self):
        # Label format is "Error — HH:MM" using the wall-clock time.
        assert re.fullmatch(r"Error — \d{2}:\d{2}", internal_error_state(NOW).menu_status_label)


# ===========================================================================
# Helper-level tests.
#
# Everything below isolates one private helper. The value here is pinning the
# arithmetic-heavy edge cases (unit boundaries, clamping, None handling) that
# would be tedious and noisy to enumerate through process().
# ===========================================================================


class TestFormatTimeLeft:
    """_format_time_left: humanizes the duration until a reset, picking the
    two most-significant units and dropping the rest."""

    def test_none_returns_unknown(self):
        # No reset timestamp known.
        assert _format_time_left(None, NOW) == "unknown"

    def test_past_time_clamps_to_zero_seconds(self):
        # A reset already in the past must not produce a negative duration.
        assert _format_time_left(NOW - timedelta(hours=1), NOW) == "0s"

    def test_seconds_only(self):
        assert _format_time_left(NOW + timedelta(seconds=45), NOW) == "45s"

    def test_minutes_and_seconds(self):
        assert _format_time_left(NOW + timedelta(minutes=5, seconds=30), NOW) == "5m 30s"

    def test_hours_and_minutes_omit_seconds(self):
        # Once we're in hours, seconds are dropped from the output.
        assert _format_time_left(NOW + timedelta(hours=2, minutes=15, seconds=30), NOW) == "2h 15m"

    def test_days_and_hours_omit_minutes(self):
        # Once we're in days, minutes are dropped.
        assert _format_time_left(NOW + timedelta(days=3, hours=4, minutes=20), NOW) == "3d 4h"

    def test_exact_minute_boundary(self):
        # Exactly 60s rolls over into the minutes format.
        assert _format_time_left(NOW + timedelta(minutes=1), NOW) == "1m 0s"

    def test_exact_hour_boundary(self):
        assert _format_time_left(NOW + timedelta(hours=1), NOW) == "1h 0m"

    def test_exact_day_boundary(self):
        assert _format_time_left(NOW + timedelta(days=1), NOW) == "1d 0h"


class TestFormatElapsed:
    """_format_elapsed: coarse "how long ago" using a single unit. Used by
    _menu_label for the tray menu freshness text."""

    def test_zero_seconds(self):
        assert _format_elapsed(0) == "0s"

    def test_under_a_minute(self):
        assert _format_elapsed(59) == "59s"

    def test_one_minute_boundary(self):
        # 60s is the first value that reports in minutes.
        assert _format_elapsed(60) == "1m"

    def test_minutes_truncate_not_round(self):
        # 125s -> 2m (integer division, no rounding up to 3m).
        assert _format_elapsed(125) == "2m"

    def test_just_under_an_hour(self):
        assert _format_elapsed(3599) == "59m"

    def test_one_hour_boundary(self):
        # 3600s is the first value that reports in hours.
        assert _format_elapsed(3600) == "1h"

    def test_hours(self):
        assert _format_elapsed(7200) == "2h"


class TestUpdatedAtLine:
    """_updated_at_line: renders the fetch time in the machine's *local* zone."""

    def test_format_is_updated_at_hh_mm_ss(self):
        assert re.fullmatch(r"Updated at \d{2}:\d{2}:\d{2}", _updated_at_line(NOW))

    def test_value_matches_local_conversion(self):
        # Asserted against the same astimezone() conversion the function uses,
        # so the test stays correct on any machine's timezone.
        expected = NOW.astimezone().strftime("%H:%M:%S")
        assert _updated_at_line(NOW) == f"Updated at {expected}"


class TestMenuLabel:
    """_menu_label: the right-click menu's status line. Branches on fetch_error
    and always reports how long ago the last (attempted) fetch was."""

    def test_no_error_recent(self):
        data = make_data(fetched_at=NOW - timedelta(seconds=10))
        assert _menu_label(data, NOW) == "Updated 10s ago"

    def test_future_fetched_at_clamps_to_zero(self):
        # Clock skew shouldn't yield a negative "ago" value.
        data = make_data(fetched_at=NOW + timedelta(seconds=30))
        assert _menu_label(data, NOW) == "Updated 0s ago"

    @pytest.mark.parametrize("error", ["timeout", "offline"])
    def test_offline_errors_share_wording(self, error):
        # Both network-ish errors collapse to the same "Offline" wording.
        data = make_data(fetch_error=error, fetched_at=NOW - timedelta(minutes=2))
        assert _menu_label(data, NOW) == "Offline — last update 2m ago"

    def test_token_expired(self):
        data = make_data(fetch_error="token_expired", fetched_at=NOW - timedelta(minutes=5))
        assert _menu_label(data, NOW) == "Token expired — last update 5m ago"

    def test_no_credentials(self):
        data = make_data(fetch_error="no_credentials", fetched_at=NOW - timedelta(hours=1))
        assert _menu_label(data, NOW) == "Not logged in — last update 1h ago"

    def test_unrecognized_error_uses_generic_wording(self):
        # Any error string we don't special-case falls back to "Error".
        data = make_data(fetch_error="bad_response", fetched_at=NOW - timedelta(seconds=5))
        assert _menu_label(data, NOW) == "Error — last update 5s ago"


class TestErrorTooltip:
    """_error_tooltip: maps a fetch_error code to the tooltip's first line."""

    def test_token_expired(self):
        data = make_data(fetch_error="token_expired")
        assert _error_tooltip("token_expired", data, NOW) == "Claude token expired — start Claude Code to refresh"

    @pytest.mark.parametrize("error", ["timeout", "offline"])
    def test_offline_includes_elapsed(self, error):
        # The offline tooltip is dynamic — it embeds how long we've been stale.
        data = make_data(fetch_error=error, fetched_at=NOW - timedelta(minutes=3))
        assert _error_tooltip(error, data, NOW) == "Offline — last update 3m ago"

    def test_no_credentials(self):
        data = make_data(fetch_error="no_credentials")
        assert _error_tooltip("no_credentials", data, NOW) == "Claude credentials not found — log in via Claude Code"

    def test_bad_response(self):
        data = make_data(fetch_error="bad_response")
        assert _error_tooltip("bad_response", data, NOW) == "Unexpected API response — see log for details"

    def test_unknown_error_falls_back_to_internal(self):
        # Defense in depth: an unmapped code still yields a sane message.
        data = make_data(fetch_error="boom")
        assert _error_tooltip("boom", data, NOW) == "Internal error — see log"
