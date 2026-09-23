from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest

from claudemonitor.config import Config, ThresholdsConfig
from claudemonitor.models import (
    CLAUDE,
    CODEX,
    DisplayState,
    FetchError,
    LabelSegment,
    Provider,
    ProviderUsageData,
    UsageWindow,
)
from claudemonitor.processor import (
    _ERROR_DISPLAY,
    _WORDING,
    _display_for,
    LOADING_MENU_STATUS,
    LOADING_TASKBAR_TEXT,
    LOADING_TOOLTIP,
    loading_label,
    taskbar_label,
    tray_status,
    _error_tooltip,
    _window_not_started,
    _format_elapsed,
    _format_time_left,
    _icon_color,
    _menu_label,
    _updated_at_line,
    _usage_lines,
    internal_error_state,
    process,
)

# The 5h rolling window only begins once the user sends their first message;
# until then the API reports utilization 0.0 with resets_at=None. This is the
# exact line we surface in place of a bogus "100% left · resets in unknown".
FIVE_HOUR_NOT_STARTED_LINE = "5h: send a message to start the session"
WEEK_NOT_STARTED_LINE = "Week: send a message to start the session"

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
) -> ProviderUsageData:
    """Build an ProviderUsageData with sensible defaults so each test only
    has to spell out the fields it actually cares about."""
    return ProviderUsageData(
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
        return process(data, NOW, Config(), CLAUDE)

    def test_icon_is_green_with_plenty_remaining(self):
        # 70% remaining > amber_below (50) -> green branch of process().
        assert self._state().icon_color == "green"

    def test_taskbar_text_shows_remaining_usage_and_reset_time(self):
        data = make_data(
            five_hour=UsageWindow(
                utilization=20.0,
                resets_at=NOW + timedelta(hours=3),
            )
        )

        assert process(data, NOW, Config(), CLAUDE).taskbar_text == "80% (3h 0m)"

    def test_taskbar_text_shows_minutes_alongside_hours(self):
        data = make_data(
            five_hour=UsageWindow(
                utilization=20.0,
                resets_at=NOW + timedelta(hours=3, minutes=45),
            )
        )

        assert process(data, NOW, Config(), CLAUDE).taskbar_text == "80% (3h 45m)"

    def test_tooltip_has_full_three_line_body_plus_timestamp(self):
        # Verifies the exact assembled tooltip: header, 5h line, week line,
        # and the trailing "Updated at" line. This is the one place we check
        # the entire multi-line layout in one shot.
        lines = self._state().tooltip.split("\n")
        assert lines[0] == "Claude usage"
        assert lines[1] == "5h:   70% left · resets in 2h 15m"
        assert lines[2] == "Week: 90% left · resets in 3d 4h"
        assert lines[3] == "Updated (15 seconds ago)"

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
        assert process(data, NOW, Config(), CLAUDE).icon_color == expected

    def test_custom_thresholds_are_respected(self):
        # With amber_below=80/red_below=40, 30% remaining falls below 40 -> red,
        # proving process() reads the config rather than hardcoding 50/20.
        config = Config(thresholds=ThresholdsConfig(amber_below=80, red_below=40))
        data = make_data(
            five_hour=UsageWindow(utilization=70.0, resets_at=NOW + timedelta(hours=1))
        )
        assert process(data, NOW, config, CLAUDE).icon_color == "red"


class TestProcessTooltipDetails:
    """Smaller tooltip behaviors that aren't covered by the happy path."""

    def test_week_line_omitted_when_no_seven_day_window(self):
        # When seven_day is None the tooltip should have no "Week:" line at all.
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2))
        )
        lines = process(data, NOW, Config(), CLAUDE).tooltip.split("\n")
        assert not any(line.startswith("Week:") for line in lines)

    def test_remaining_percentage_is_rounded_to_whole_number(self):
        # 100 - 33.6 = 66.4, formatted with "{:.0f}" -> "66%".
        data = make_data(
            five_hour=UsageWindow(utilization=33.6, resets_at=NOW + timedelta(hours=1))
        )
        assert "66% left" in process(data, NOW, Config(), CLAUDE).tooltip

    def test_unknown_reset_when_resets_at_is_none(self):
        # A window with no reset timestamp should surface "unknown" (from
        # _format_time_left) rather than crashing or showing a bogus duration.
        data = make_data(five_hour=UsageWindow(utilization=10.0, resets_at=None))
        assert "resets in unknown" in process(data, NOW, Config(), CLAUDE).tooltip


class TestProcessFiveHourNotStarted:
    """The 5h window has not started yet. Before the user's first message the
    API returns the five_hour window with utilization 0.0 and resets_at=None.
    Treating that literally produced a misleading "100% left · resets in
    unknown"; instead we tell the user the countdown hasn't started.

    Mirrors the real API payload: a not-started 5h window alongside a live
    weekly window."""

    def _data(self, fetched_at=NOW):
        return make_data(
            five_hour=UsageWindow(utilization=0.0, resets_at=None),
            seven_day=UsageWindow(
                utilization=5.0, resets_at=NOW + timedelta(days=4, hours=3)
            ),
            fetched_at=fetched_at,
        )

    def test_five_hour_line_explains_countdown_not_started(self):
        lines = process(self._data(), NOW, Config(), CLAUDE).tooltip.split("\n")
        assert lines[0] == "Claude usage"
        assert lines[1] == FIVE_HOUR_NOT_STARTED_LINE

    def test_does_not_show_bogus_full_window(self):
        # The old behavior leaked through as "100% left" / "resets in unknown".
        tooltip = process(self._data(), NOW, Config(), CLAUDE).tooltip
        assert "100% left" not in tooltip
        assert "resets in unknown" not in tooltip

    def test_weekly_window_still_shown_normally(self):
        # Only the 5h line changes; the live weekly window renders as usual.
        lines = process(self._data(), NOW, Config(), CLAUDE).tooltip.split("\n")
        assert lines[2] == "Week: 95% left · resets in 4d 3h"

    def test_week_line_explains_when_weekly_session_has_not_started(self):
        data = make_data(
            five_hour=UsageWindow(utilization=0.0, resets_at=None),
            seven_day=UsageWindow(utilization=0.0, resets_at=None),
        )
        lines = process(data, NOW, Config(), CLAUDE).tooltip.split("\n")
        assert lines[1] == WEEK_NOT_STARTED_LINE
        assert not any(line.startswith("5h:") for line in lines)

    def test_icon_is_green_because_full_usage_is_available(self):
        # Nothing has been spent yet, so the user has their whole 5h budget.
        assert process(self._data(), NOW, Config(), CLAUDE).icon_color == "green"

    def test_tooltip_still_ends_with_updated_line(self):
        data = self._data(fetched_at=NOW - timedelta(seconds=15))
        last = process(data, NOW, Config(), CLAUDE).tooltip.split("\n")[-1]
        assert last == "Updated (15 seconds ago)"

    def test_tooltip_fits_windows_tooltip_limit(self):
        assert len(process(self._data(), NOW, Config(), CLAUDE).tooltip) <= 128


class TestProcessNoData:
    """No fetch error, but the API returned no 5h window — a distinct grey
    state separate from the error states."""

    def test_missing_five_hour_is_grey_with_explanatory_tooltip(self):
        data = make_data(five_hour=None)
        state = process(data, NOW, Config(), CLAUDE)
        assert state.icon_color == "grey"
        assert "No usage data available" in state.tooltip

    def test_no_data_tooltip_still_ends_with_updated_line(self):
        data = make_data(five_hour=None)
        last_line = process(data, NOW, Config(), CLAUDE).tooltip.split("\n")[-1]
        assert last_line == "Updated (0 seconds ago)"


class TestProcessErrors:
    """When fetch_error is set, process() short-circuits to a grey icon and an
    error tooltip before ever looking at the usage windows. These cases drive
    _error_tooltip and the error branches of _menu_label."""

    def test_error_yields_grey_icon(self):
        data = make_data(fetch_error="timeout", fetched_at=NOW - timedelta(minutes=1))
        assert process(data, NOW, Config(), CLAUDE).icon_color == "grey"

    def test_error_tooltip_is_message_then_updated_line(self):
        # First line is the human-readable error, last line is the timestamp.
        data = make_data(fetch_error="token_expired", fetched_at=NOW)
        lines = process(data, NOW, Config(), CLAUDE).tooltip.split("\n")
        assert lines[0] == "Claude token expired — start Claude Code to refresh"
        assert lines[-1] == "Updated (0 seconds ago)"

    def test_error_sets_matching_menu_label(self):
        data = make_data(fetch_error="no_credentials", fetched_at=NOW - timedelta(minutes=2))
        assert process(data, NOW, Config(), CLAUDE).menu_status_label == "Not logged in — last update 2m ago"

    def test_error_takes_precedence_over_present_usage_data(self):
        # Even with a perfectly good five_hour window, an error must win and
        # produce grey — proving the error check happens first.
        data = make_data(
            five_hour=UsageWindow(utilization=10.0, resets_at=NOW + timedelta(hours=1)),
            fetch_error="bad_response",
        )
        assert process(data, NOW, Config(), CLAUDE).icon_color == "grey"


class TestProcessRateLimited:
    """HTTP 429 handling (fetch_error == "rate_limited").

    Unlike other errors, a rate-limit does not mean our data is wrong — just
    that we couldn't refresh it. So instead of going grey, process() falls back
    to the last successful result and flags it as stale. Without any prior good
    data it degrades to the same grey treatment as other errors."""

    def _last_good(self):
        # A fully-populated successful result captured two minutes before NOW.
        return make_data(
            five_hour=UsageWindow(
                utilization=30.0, resets_at=NOW + timedelta(hours=2, minutes=15)
            ),
            seven_day=UsageWindow(
                utilization=10.0, resets_at=NOW + timedelta(days=3, hours=4)
            ),
            fetched_at=NOW - timedelta(minutes=2),
        )

    def test_shows_last_good_usage_color(self):
        # 70% remaining -> green, computed from last_good rather than greyed out.
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        state = process(data, NOW, Config(), CLAUDE, last_good=self._last_good())
        assert state.icon_color == "green"

    def test_tooltip_shows_last_good_usage_lines(self):
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        lines = process(data, NOW, Config(), CLAUDE, last_good=self._last_good()).tooltip.split("\n")
        assert lines[0] == "Claude usage"
        assert lines[1] == "5h:   70% left · resets in 2h 15m"
        assert lines[2] == "Week: 90% left · resets in 3d 4h"

    def test_tooltip_flags_unable_to_fetch_recent_data(self):
        # The user-facing message requested in step 1.
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        tooltip = process(data, NOW, Config(), CLAUDE, last_good=self._last_good()).tooltip
        assert "Unable to fetch recent data" in tooltip

    def test_stale_note_uses_elapsed_since_last_good_fetch(self):
        # The note shows how long ago the last *successful* fetch was (relative,
        # not an absolute clock time), measured from that fetch — not the failed
        # 429 attempt. Kept compact so the whole tooltip fits Windows' 128-char
        # tray-tooltip limit.
        last_good = self._last_good()  # fetched 2 minutes before NOW
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        last_line = process(data, NOW, Config(), CLAUDE, last_good=last_good).tooltip.split("\n")[-1]
        assert last_line == "Unable to fetch recent data (2m ago)"

    def test_stale_tooltip_fits_windows_tooltip_limit(self):
        # Worst case: 3-digit percentages and wide reset strings. Must stay
        # within the 128-char NOTIFYICONDATAW.szTip buffer.
        last_good = make_data(
            five_hour=UsageWindow(utilization=100.0, resets_at=NOW + timedelta(hours=2, minutes=15)),
            seven_day=UsageWindow(utilization=100.0, resets_at=NOW + timedelta(days=6, hours=23)),
            fetched_at=NOW - timedelta(minutes=2),
        )
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        tooltip = process(data, NOW, Config(), CLAUDE, last_good=last_good).tooltip
        assert len(tooltip) <= 128

    def test_menu_label_reports_rate_limited_since_last_good(self):
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        state = process(data, NOW, Config(), CLAUDE, last_good=self._last_good())
        assert state.menu_status_label == "Rate limited — last update 2m ago"

    def test_without_last_good_falls_back_to_grey(self):
        data = make_data(fetch_error="rate_limited", fetched_at=NOW - timedelta(seconds=5))
        state = process(data, NOW, Config(), CLAUDE, last_good=None)
        assert state.icon_color == "grey"

    def test_without_last_good_uses_rate_limited_messaging(self):
        data = make_data(fetch_error="rate_limited", fetched_at=NOW - timedelta(seconds=5))
        state = process(data, NOW, Config(), CLAUDE, last_good=None)
        assert state.menu_status_label == "Rate limited — last update 5s ago"
        assert state.tooltip.split("\n")[0] == "Rate limited — too many requests, will retry"

    def test_last_good_without_usage_window_falls_back_to_grey(self):
        # A "successful" fetch that returned no five_hour window isn't useful
        # enough to display, so we treat it as if we had nothing.
        last_good = make_data(five_hour=None, fetched_at=NOW - timedelta(minutes=1))
        data = make_data(fetch_error="rate_limited", fetched_at=NOW)
        assert process(data, NOW, Config(), CLAUDE, last_good=last_good).icon_color == "grey"


# ===========================================================================
# internal_error_state — the hard-coded fallback used when process() itself
# (or its caller) blows up. No inputs beyond `now`, so just two assertions.
# ===========================================================================


class TestInternalErrorState:
    def test_grey_icon_and_fixed_tooltip(self):
        state = internal_error_state(NOW, CLAUDE)
        assert state.icon_color == "grey"
        assert state.tooltip == "Internal error — see log"

    def test_menu_label_is_error_with_hh_mm(self):
        # Label format is "Error — HH:MM" using the wall-clock time.
        assert re.fullmatch(r"Error — \d{2}:\d{2}", internal_error_state(NOW, CLAUDE).menu_status_label)


# ===========================================================================
# Helper-level tests.
#
# Everything below isolates one private helper. The value here is pinning the
# arithmetic-heavy edge cases (unit boundaries, clamping, None handling) that
# would be tedious and noisy to enumerate through process().
# ===========================================================================


class TestIconColor:
    """_icon_color: maps a 5h utilization (used %) to a color via the configured
    thresholds on the *remaining* percentage. The comparisons are asymmetric:
        remaining >  amber_below -> green
        remaining >= red_below   -> amber
        otherwise                -> red
    so the boundary values are the interesting cases."""

    @pytest.mark.parametrize(
        "utilization,expected",
        [
            (0.0, "green"),    # 100% remaining
            (49.0, "green"),   # 51% remaining, strictly > 50
            (50.0, "amber"),   # exactly 50% remaining: NOT > 50 -> amber
            (50.1, "amber"),   # 49.9% remaining
            (80.0, "amber"),   # exactly 20% remaining == red_below -> amber
            (80.1, "red"),     # 19.9% remaining -> red
            (100.0, "red"),    # 0% remaining
        ],
    )
    def test_color_by_remaining(self, utilization, expected):
        assert _icon_color(utilization, Config()) == expected

    def test_reads_custom_thresholds(self):
        # amber_below=80/red_below=40: 30% remaining falls below 40 -> red,
        # proving the helper uses config rather than hardcoding 50/20.
        config = Config(thresholds=ThresholdsConfig(amber_below=80, red_below=40))
        assert _icon_color(70.0, config) == "red"


class TestUsageLines:
    """_usage_lines: builds the 'Claude usage' header plus the 5h (and optional
    weekly) "% left · resets in ..." lines, without the trailing status line."""

    def test_five_hour_only_has_header_and_one_window(self):
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2, minutes=15))
        )
        assert _usage_lines(data, NOW, CLAUDE) == [
            "Claude usage",
            "5h:   70% left · resets in 2h 15m",
        ]

    def test_includes_week_line_when_seven_day_present(self):
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2, minutes=15)),
            seven_day=UsageWindow(utilization=10.0, resets_at=NOW + timedelta(days=3, hours=4)),
        )
        assert _usage_lines(data, NOW, CLAUDE)[2] == "Week: 90% left · resets in 3d 4h"

    def test_omits_week_line_when_no_seven_day(self):
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2))
        )
        assert not any(line.startswith("Week:") for line in _usage_lines(data, NOW, CLAUDE))

    def test_does_not_append_updated_line(self):
        # The caller owns the trailing status line; this helper must not add one.
        data = make_data(
            five_hour=UsageWindow(utilization=30.0, resets_at=NOW + timedelta(hours=2))
        )
        assert not any(line.startswith("Updated at") for line in _usage_lines(data, NOW, CLAUDE))

    def test_percentage_is_rounded_to_whole_number(self):
        # 100 - 33.6 = 66.4 -> "66%".
        data = make_data(
            five_hour=UsageWindow(utilization=33.6, resets_at=NOW + timedelta(hours=1))
        )
        assert _usage_lines(data, NOW, CLAUDE)[1] == "5h:   66% left · resets in 1h 0m"

    def test_not_started_window_uses_explanatory_five_hour_line(self):
        data = make_data(five_hour=UsageWindow(utilization=0.0, resets_at=None))
        assert _usage_lines(data, NOW, CLAUDE)[1] == FIVE_HOUR_NOT_STARTED_LINE

    def test_not_started_does_not_affect_week_line(self):
        data = make_data(
            five_hour=UsageWindow(utilization=0.0, resets_at=None),
            seven_day=UsageWindow(utilization=10.0, resets_at=NOW + timedelta(days=3, hours=4)),
        )
        lines = _usage_lines(data, NOW, CLAUDE)
        assert lines[1] == FIVE_HOUR_NOT_STARTED_LINE
        assert lines[2] == "Week: 90% left · resets in 3d 4h"


class TestWindowNotStarted:
    """_window_not_started recognizes a window with no active session."""

    def test_true_when_zero_utilization_and_no_reset(self):
        assert _window_not_started(UsageWindow(utilization=0.0, resets_at=None)) is True

    def test_false_when_window_is_active(self):
        window = UsageWindow(utilization=0.0, resets_at=NOW + timedelta(hours=5))
        assert _window_not_started(window) is False

    def test_false_when_usage_has_accrued_even_without_reset(self):
        # A window with real usage but a missing reset time is a different
        # (degenerate) case — it should still report its percentage, not be
        # mistaken for a not-yet-started window.
        assert _window_not_started(UsageWindow(utilization=10.0, resets_at=None)) is False


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
    """_updated_at_line: renders the fetch freshness in whole seconds."""

    def test_format_is_relative_seconds(self):
        assert _updated_at_line(NOW - timedelta(seconds=15), NOW) == "Updated (15 seconds ago)"

    def test_future_fetch_time_clamps_to_zero_seconds(self):
        assert _updated_at_line(NOW + timedelta(seconds=30), NOW) == "Updated (0 seconds ago)"


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

    def test_rate_limited(self):
        data = make_data(fetch_error="rate_limited", fetched_at=NOW - timedelta(seconds=30))
        assert _menu_label(data, NOW) == "Rate limited — last update 30s ago"

    def test_a_bad_response_uses_the_generic_wording(self):
        data = make_data(fetch_error="bad_response", fetched_at=NOW - timedelta(seconds=5))
        assert _menu_label(data, NOW) == "Error — last update 5s ago"

    def test_an_unnamed_failure_uses_the_generic_wording(self):
        data = make_data(fetch_error="unknown", fetched_at=NOW - timedelta(seconds=5))
        assert _menu_label(data, NOW) == "Error — last update 5s ago"

    def test_a_poller_that_has_not_fetched_yet_uses_the_generic_wording(self):
        data = make_data(fetch_error="no_data", fetched_at=NOW - timedelta(seconds=5))
        assert _menu_label(data, NOW) == "Error — last update 5s ago"


class TestOneRowPerError:
    """Every fetch error has one row, and the row says all three things.

    The taskbar label, the menu status line, and the tooltip used to be three
    tables written in the same order, which is how one of them came to be
    missing a value the other two had.
    """

    def test_every_error_the_model_allows_has_a_row(self):
        assert set(_ERROR_DISPLAY) == set(get_args(FetchError))

    def test_no_row_is_left_blank(self):
        for error, display in _ERROR_DISPLAY.items():
            assert display.taskbar_text, error
            assert display.menu_prefix, error
            assert display.tooltip(_WORDING["claude"], "1m"), error

    def test_the_taskbar_label_is_short_enough_to_read_at_a_glance(self):
        # It shares one row with a provider's mark on a taskbar segment.
        assert all(len(display.taskbar_text) <= 16 for display in _ERROR_DISPLAY.values())

    def test_an_exhausted_refresh_replaces_the_expired_token_row(self):
        expired = _display_for("token_expired", session_refresh_exhausted=False)
        exhausted = _display_for("token_expired", session_refresh_exhausted=True)

        assert exhausted.taskbar_text == "sign in"
        assert exhausted != expired

    def test_only_an_expired_token_is_replaced(self):
        # A nudge can exhaust itself on a missing CLI while fetches succeed,
        # and no other error is something a sign-in would fix.
        for error in get_args(FetchError):
            if error == "token_expired":
                continue
            assert _display_for(error, session_refresh_exhausted=True) is _ERROR_DISPLAY[error]

    def test_each_provider_is_named_in_its_own_tooltip(self):
        for error in ("token_expired", "no_credentials"):
            claude = _ERROR_DISPLAY[error].tooltip(_WORDING["claude"], "1m")
            codex = _ERROR_DISPLAY[error].tooltip(_WORDING["codex"], "1m")
            assert "Claude" in claude and "Codex" in codex


class TestErrorTooltip:
    """_error_tooltip: maps a fetch_error code to the tooltip's first line."""

    def test_token_expired(self):
        data = make_data(fetch_error="token_expired")
        assert _error_tooltip("token_expired", data, NOW, CLAUDE) == "Claude token expired — start Claude Code to refresh"

    @pytest.mark.parametrize("error", ["timeout", "offline"])
    def test_offline_includes_elapsed(self, error):
        # The offline tooltip is dynamic — it embeds how long we've been stale.
        data = make_data(fetch_error=error, fetched_at=NOW - timedelta(minutes=3))
        assert _error_tooltip(error, data, NOW, CLAUDE) == "Offline — last update 3m ago"

    def test_no_credentials(self):
        data = make_data(fetch_error="no_credentials")
        assert _error_tooltip("no_credentials", data, NOW, CLAUDE) == "Claude credentials not found — log in via Claude Code"

    def test_bad_response(self):
        data = make_data(fetch_error="bad_response")
        assert _error_tooltip("bad_response", data, NOW, CLAUDE) == "Unexpected API response — see log for details"

    def test_rate_limited(self):
        data = make_data(fetch_error="rate_limited")
        assert _error_tooltip("rate_limited", data, NOW, CLAUDE) == "Rate limited — too many requests, will retry"

    def test_unknown_falls_back_to_internal(self):
        # Whatever the fetcher could not name is still shown as something.
        data = make_data(fetch_error="unknown")
        assert _error_tooltip("unknown", data, NOW, CLAUDE) == "Internal error — see log"

    def test_no_data_says_there_is_none_yet(self):
        data = make_data(fetch_error="no_data")
        assert _error_tooltip("no_data", data, NOW, CLAUDE) == "No usage data yet"


class TestTaskbarText:
    """The taskbar label is the most visible surface, so every fetch outcome —
    success, each error code, and missing data — must produce its own honest
    short message rather than one undifferentiated placeholder."""

    def _taskbar_text(self, **kwargs) -> str:
        return process(make_data(**kwargs), NOW, Config(), CLAUDE).taskbar_text

    def test_remaining_usage_is_floored_so_full_only_means_untouched(self):
        # 99.6% remaining must not round up to a reassuring "100%".
        assert (
            self._taskbar_text(
                five_hour=UsageWindow(utilization=0.4, resets_at=NOW + timedelta(hours=2))
            )
            == "99% (2h 0m)"
        )

    def test_reset_under_one_minute_avoids_a_zero_minute_countdown(self):
        assert (
            self._taskbar_text(
                five_hour=UsageWindow(utilization=20.0, resets_at=NOW + timedelta(seconds=30))
            )
            == "80% (under a minute)"
        )

    def test_reset_within_the_hour_is_shown_in_minutes(self):
        assert (
            self._taskbar_text(
                five_hour=UsageWindow(utilization=20.0, resets_at=NOW + timedelta(minutes=1))
            )
            == "80% (1m)"
        )

    def test_unstarted_window_matches_the_tooltip_definition(self):
        # _window_not_started requires utilization 0 *and* no reset time, so the
        # taskbar must use the same rule the tooltip does.
        assert (
            self._taskbar_text(five_hour=UsageWindow(utilization=0.0, resets_at=None))
            == "100% (not started)"
        )

    def test_used_window_without_a_reset_time_is_not_called_unstarted(self):
        # The tooltip says "resets in unknown" here; the taskbar must agree
        # rather than claiming the session never started.
        assert (
            self._taskbar_text(five_hour=UsageWindow(utilization=40.0, resets_at=None))
            == "60% (unknown)"
        )

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ("token_expired", "token expired"),
            ("timeout", "offline"),
            ("offline", "offline"),
            ("no_credentials", "not logged in"),
            ("bad_response", "bad response"),
            ("rate_limited", "rate limited"),
            ("no_data", "no data"),
            ("unknown", "unavailable"),
        ],
    )
    def test_each_fetch_error_gets_its_own_short_label(self, error, expected):
        assert self._taskbar_text(fetch_error=error) == expected

    def test_missing_usage_data_is_reported_as_no_data(self):
        assert self._taskbar_text(five_hour=None) == "no data"

    def test_internal_error_state_reports_an_error_label(self):
        assert internal_error_state(NOW, CLAUDE).taskbar_text == "error"

    def test_rate_limited_fallback_keeps_showing_the_last_good_usage(self):
        last_good = make_data(
            five_hour=UsageWindow(utilization=25.0, resets_at=NOW + timedelta(hours=2)),
            fetched_at=NOW - timedelta(minutes=5),
        )
        data = make_data(fetch_error="rate_limited")

        state = process(data, NOW, Config(), CLAUDE, last_good=last_good)

        assert state.taskbar_text == "75% (2h 0m)"


class TestSignInNeededWording:
    """Once the CLI nudge has given up, "start Claude Code to refresh" is advice
    that cannot work — the credentials are dead and only a sign-in fixes them."""

    def _state(self, *, exhausted: bool, fetched_at: datetime = NOW):
        data = make_data(fetch_error="token_expired", fetched_at=fetched_at)
        return process(data, NOW, Config(), CLAUDE, session_refresh_exhausted=exhausted)

    def test_tooltip_asks_the_user_to_sign_in(self):
        lines = self._state(exhausted=True).tooltip.split("\n")
        assert lines[0] == "Claude sign-in needed — run: claude /login"

    def test_menu_label_reports_the_sign_in(self):
        state = self._state(exhausted=True, fetched_at=NOW - timedelta(minutes=5))
        assert state.menu_status_label == "Sign-in needed — last update 5m ago"

    def test_taskbar_asks_for_a_sign_in(self):
        assert self._state(exhausted=True).taskbar_text == "sign in"

    def test_icon_stays_grey(self):
        assert self._state(exhausted=True).icon_color == "grey"

    def test_untripped_breaker_keeps_the_original_wording(self):
        state = self._state(exhausted=False, fetched_at=NOW - timedelta(minutes=5))
        assert state.tooltip.split("\n")[0] == (
            "Claude token expired — start Claude Code to refresh"
        )
        assert state.menu_status_label == "Token expired — last update 5m ago"
        assert state.taskbar_text == "token expired"

    def test_the_flag_defaults_to_the_original_wording(self):
        data = make_data(fetch_error="token_expired")
        assert process(data, NOW, Config(), CLAUDE).taskbar_text == "token expired"

    def test_other_errors_are_unaffected_by_the_tripped_breaker(self):
        # The breaker can also trip on a missing CLI while fetches succeed, so a
        # tripped flag must not relabel errors a sign-in would not fix.
        data = make_data(fetch_error="no_credentials", fetched_at=NOW)
        state = process(data, NOW, Config(), CLAUDE, session_refresh_exhausted=True)
        assert state.taskbar_text == "not logged in"
        assert state.tooltip.split("\n")[0] == (
            "Claude credentials not found — log in via Claude Code"
        )

    def test_successful_usage_is_unaffected_by_the_tripped_breaker(self):
        data = make_data(
            five_hour=UsageWindow(utilization=20.0, resets_at=NOW + timedelta(hours=1))
        )
        state = process(data, NOW, Config(), CLAUDE, session_refresh_exhausted=True)
        assert state.taskbar_text == "80% (1h 0m)"
        assert state.icon_color == "green"


# ===========================================================================
# Two providers — Codex wording, and combining both into one taskbar label.
# ===========================================================================


class TestCodexProvider:
    """process() must speak about Codex in Codex's own words."""

    def _usage(self) -> ProviderUsageData:
        return make_data(
            five_hour=UsageWindow(
                utilization=20.0, resets_at=NOW + timedelta(hours=2)
            ),
            seven_day=UsageWindow(utilization=40.0, resets_at=NOW + timedelta(days=3)),
        )

    def test_state_is_tagged_with_its_provider(self):
        state = process(self._usage(), NOW, Config(), CODEX)

        assert state.provider is CODEX

    def test_tooltip_heading_names_codex(self):
        tooltip = process(self._usage(), NOW, Config(), CODEX).tooltip

        assert tooltip.split("\n")[0] == "Codex usage"

    def test_percentages_are_formatted_the_same_as_claude(self):
        codex = process(self._usage(), NOW, Config(), CODEX)
        claude = process(self._usage(), NOW, Config(), CLAUDE)

        assert codex.taskbar_text == claude.taskbar_text
        assert codex.icon_color == claude.icon_color

    @pytest.mark.parametrize(
        "error, expected",
        [
            ("token_expired", "Codex token expired — start Codex to refresh"),
            ("no_credentials", "Codex credentials not found — log in via Codex"),
        ],
    )
    def test_error_tooltips_name_codex(self, error, expected):
        state = process(make_data(fetch_error=error), NOW, Config(), CODEX)

        assert state.tooltip.split("\n")[0] == expected

    def test_exhausted_refresh_asks_for_the_codex_login_command(self):
        state = process(
            make_data(fetch_error="token_expired"),
            NOW,
            Config(),
            CODEX,
            session_refresh_exhausted=True,
        )

        assert "codex login" in state.tooltip

    def test_claude_wording_is_unchanged_by_the_second_provider(self):
        state = process(make_data(fetch_error="token_expired"), NOW, Config(), CLAUDE)

        assert state.tooltip.split("\n")[0] == (
            "Claude token expired — start Claude Code to refresh"
        )


class TestTaskbarLabelCombining:
    """The single taskbar label carries one segment per visible provider."""

    def _state(self, provider, taskbar_text: str, tooltip: str) -> DisplayState:
        return DisplayState(
            provider=provider,
            icon_color="green",
            tooltip=tooltip,
            menu_status_label="Updated 0s ago",
            taskbar_text=taskbar_text,
            tray_text=taskbar_text,
        )

    def test_one_segment_per_provider_in_order(self):
        label = taskbar_label(
            [
                self._state(CLAUDE, "87% (2h 10m)", "Claude usage"),
                self._state(CODEX, "64% (3h 5m)", "Codex usage"),
            ]
        )

        assert label.segments == [
            LabelSegment(provider=CLAUDE, text="87% (2h 10m)"),
            LabelSegment(provider=CODEX, text="64% (3h 5m)"),
        ]

    def test_tooltip_stacks_both_providers_with_a_blank_line_between(self):
        label = taskbar_label(
            [
                self._state(CLAUDE, "87%", "Claude usage\n5h: 87% left"),
                self._state(CODEX, "64%", "Codex usage\n5h: 64% left"),
            ]
        )

        assert label.tooltip == (
            "Claude usage\n5h: 87% left\n\nCodex usage\n5h: 64% left"
        )

    def test_a_single_provider_needs_no_separator(self):
        label = taskbar_label([self._state(CLAUDE, "87%", "Claude usage")])

        assert label.segments == [LabelSegment(provider=CLAUDE, text="87%")]
        assert label.tooltip == "Claude usage"

    def test_no_providers_falls_back_to_the_loading_label(self):
        # An empty label would collapse the native window to nothing; showing
        # the loading text keeps it measurable until a provider reports in.
        label = taskbar_label([])

        assert label.segments == [
            LabelSegment(provider=CLAUDE, text=LOADING_TASKBAR_TEXT)
        ]


class TestLoadingLabel:
    """The label shown before the first fetch completes."""

    def test_loading_label_has_one_segment_per_provider(self):
        label = loading_label([CLAUDE, CODEX])

        assert label.segments == [
            LabelSegment(provider=CLAUDE, text=LOADING_TASKBAR_TEXT),
            LabelSegment(provider=CODEX, text=LOADING_TASKBAR_TEXT),
        ]
        assert "loading" in label.tooltip


# ===========================================================================
# tray_status — every tracked provider, condensed onto the single tray icon.
# ===========================================================================


def _state(
    provider: Provider = CLAUDE,
    icon_color: str = "green",
    tray_text: str = "80% (3h 0m)",
    menu_status_label: str = "Updated 1s ago",
) -> DisplayState:
    """Build one provider's display state with only the tray fields spelled out."""
    return DisplayState(
        provider=provider,
        icon_color=icon_color,
        tooltip=f"{provider.label} usage",
        menu_status_label=menu_status_label,
        taskbar_text=tray_text,
        tray_text=tray_text,
    )


class TestTrayStatusColor:
    """One icon serves both providers, so it has to show the worse of the two."""

    def test_two_healthy_providers_stay_green(self):
        status = tray_status([_state(CLAUDE, "green"), _state(CODEX, "green")])

        assert status.icon_color == "green"

    def test_either_provider_running_low_colours_the_icon(self):
        for states in (
            [_state(CLAUDE, "green"), _state(CODEX, "red")],
            [_state(CLAUDE, "red"), _state(CODEX, "green")],
        ):
            assert tray_status(states).icon_color == "red"

    def test_red_outranks_amber(self):
        status = tray_status([_state(CLAUDE, "amber"), _state(CODEX, "red")])

        assert status.icon_color == "red"

    def test_a_broken_provider_greys_an_otherwise_healthy_icon(self):
        # Grey means "we do not know", which a green icon would hide.
        status = tray_status([_state(CLAUDE, "green"), _state(CODEX, "grey")])

        assert status.icon_color == "grey"

    def test_running_out_still_outranks_not_knowing(self):
        status = tray_status([_state(CLAUDE, "red"), _state(CODEX, "grey")])

        assert status.icon_color == "red"


class TestTrayStatusTooltip:
    """Hovering one icon has to say which provider each number belongs to."""

    def test_every_provider_is_named_on_its_own_line(self):
        status = tray_status(
            [
                _state(CLAUDE, tray_text="80% (3h 0m)"),
                _state(CODEX, tray_text="43% (2h 0m)"),
            ]
        )

        assert status.tooltip.splitlines() == [
            "Claude  80% (3h 0m)",
            "Codex  43% (2h 0m)",
        ]

    def test_one_provider_still_names_itself(self):
        status = tray_status([_state(CLAUDE, tray_text="80% (3h 0m)")])

        assert status.tooltip == "Claude  80% (3h 0m)"

    def test_the_tooltip_fits_the_windows_tray_limit(self):
        # NOTIFYICONDATAW.szTip holds 128 characters; pystray raises above it.
        status = tray_status(
            [
                _state(CLAUDE, tray_text="100% (not started) · week 100%"),
                _state(CODEX, tray_text="100% (not started) · week 100%"),
            ]
        )

        assert len(status.tooltip) <= 127


class TestTrayStatusMenu:
    """The menu carries the freshness line each provider used to have alone."""

    def test_one_status_line_per_provider(self):
        status = tray_status(
            [
                _state(CLAUDE, menu_status_label="Updated 5s ago"),
                _state(CODEX, menu_status_label="Rate limited — last update 2m ago"),
            ]
        )

        assert status.status_lines == [
            "Claude — Updated 5s ago",
            "Codex — Rate limited — last update 2m ago",
        ]

    def test_a_provider_names_itself_rather_than_being_looked_up(self):
        # The state carries its provider, so there is no key to fail to resolve.
        status = tray_status([_state(CODEX)])

        assert status.status_lines == ["Codex — Updated 1s ago"]


class TestTrayStatusBeforeTheFirstFetch:
    """Nothing to show yet must still produce a usable icon and menu."""

    def test_no_providers_shows_the_loading_placeholder(self):
        status = tray_status([])

        assert status.icon_color == "grey"
        assert status.tooltip == LOADING_TOOLTIP
        assert status.status_lines == [LOADING_MENU_STATUS]


class TestTrayText:
    """The one-line summary the shared tooltip stacks, built with the rest."""

    def test_it_carries_the_five_hour_reading_and_the_week(self):
        state = process(
            make_data(
                five_hour=UsageWindow(
                    utilization=20.0, resets_at=NOW + timedelta(hours=3)
                ),
                seven_day=UsageWindow(
                    utilization=36.0, resets_at=NOW + timedelta(days=3)
                ),
            ),
            NOW,
            Config(),
            CLAUDE,
        )

        assert state.tray_text == "80% (3h 0m) · week 64%"

    def test_a_provider_without_a_weekly_window_shows_only_the_five_hour(self):
        state = process(
            make_data(
                five_hour=UsageWindow(
                    utilization=20.0, resets_at=NOW + timedelta(hours=3)
                )
            ),
            NOW,
            Config(),
            CLAUDE,
        )

        assert state.tray_text == "80% (3h 0m)"

    def test_an_unstarted_week_is_not_reported_as_a_number(self):
        state = process(
            make_data(
                five_hour=UsageWindow(
                    utilization=20.0, resets_at=NOW + timedelta(hours=3)
                ),
                seven_day=UsageWindow(utilization=0.0, resets_at=None),
            ),
            NOW,
            Config(),
            CLAUDE,
        )

        assert state.tray_text == "80% (3h 0m)"

    def test_an_error_says_what_went_wrong(self):
        state = process(make_data(fetch_error="offline"), NOW, Config(), CLAUDE)

        assert state.tray_text == "offline"

    def test_missing_usage_says_so(self):
        state = process(make_data(), NOW, Config(), CLAUDE)

        assert state.tray_text == "no data"

    def test_an_internal_error_says_so(self):
        assert internal_error_state(NOW, CLAUDE).tray_text == "error"

    def test_stale_data_keeps_the_last_good_reading(self):
        last_good = make_data(
            five_hour=UsageWindow(utilization=20.0, resets_at=NOW + timedelta(hours=3)),
            seven_day=UsageWindow(utilization=36.0, resets_at=NOW + timedelta(days=3)),
            fetched_at=NOW - timedelta(minutes=2),
        )

        state = process(
            make_data(fetch_error="rate_limited"),
            NOW,
            Config(),
            CLAUDE,
            last_good=last_good,
        )

        assert state.tray_text == "80% (3h 0m) · week 64%"
