"""Coverage for the poll cycle — the loop that drives the whole application.

Every test here starts at a fetch result and finishes at what the display is
asked to show, so the composition itself is exercised rather than only the
arithmetic helpers it calls.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest

from claudemonitor.config import Config
from claudemonitor.models import AnthropicUsageData, DisplayState, UsageWindow
from claudemonitor.notifications import ThresholdNotifier
from claudemonitor.poll_cycle import PollCycle, _next_poll_interval_seconds

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def usage(
    *,
    utilization: float | None = None,
    fetch_error: str | None = None,
    status_code: int | None = 200,
    retry_after_seconds: int | None = None,
    fetched_at: datetime = NOW,
) -> AnthropicUsageData:
    """Build one fetch result, with a 5h window only when a utilization is given."""
    five_hour = (
        UsageWindow(utilization=utilization, resets_at=NOW + timedelta(hours=2))
        if utilization is not None
        else None
    )
    return AnthropicUsageData(
        five_hour=five_hour,
        fetch_error=fetch_error,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        fetched_at=fetched_at,
    )


class _RecordingDisplay:
    """Capture what the tray and taskbar would be asked to show."""

    def __init__(
        self,
        *,
        apply_raises: Exception | None = None,
        notify_raises: Exception | None = None,
    ) -> None:
        self.states: list[DisplayState] = []
        self.notifications: list[tuple[str, str]] = []
        self._apply_raises = apply_raises
        self._notify_raises = notify_raises

    def apply(self, state: DisplayState) -> None:
        self.states.append(state)
        if self._apply_raises is not None:
            raise self._apply_raises

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))
        if self._notify_raises is not None:
            raise self._notify_raises


class _RecordingNudger:
    """Record every fetch offered to the session nudge, and report a breaker."""

    def __init__(self, exhausted: bool = False) -> None:
        self.seen: list[AnthropicUsageData] = []
        self.exhausted = exhausted

    def maybe_nudge(self, data: AnthropicUsageData) -> bool:
        self.seen.append(data)
        return False


def build_cycle(
    *,
    results: list,
    display: _RecordingDisplay | None = None,
    nudger: _RecordingNudger | None = None,
    notifier: ThresholdNotifier | None = None,
    config: Config | None = None,
) -> tuple[PollCycle, _RecordingDisplay]:
    """Build a cycle whose fetches replay a scripted list of results or errors."""
    display = display or _RecordingDisplay()
    pending = iter(results)

    def fetch() -> AnthropicUsageData:
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    cycle = PollCycle(
        config or Config(),
        fetch=fetch,
        display=display,
        notifier=notifier or ThresholdNotifier(),
        nudger=nudger or _RecordingNudger(),
        now=lambda: NOW,
    )
    return cycle, display


# ===========================================================================
# run_once — one whole fetch -> process -> display cycle.
# ===========================================================================


class TestRunOnceHappyPath:
    def test_successful_usage_reaches_the_display(self):
        cycle, display = build_cycle(results=[usage(utilization=20.0)])

        cycle.run_once()

        assert display.states[-1].taskbar_text == "80% (2h 0m)"
        assert display.states[-1].icon_color == "green"

    def test_run_once_returns_the_state_it_displayed(self):
        cycle, display = build_cycle(results=[usage(utilization=20.0)])

        assert cycle.run_once() is display.states[-1]

    def test_each_fetch_error_reaches_the_display_as_its_own_message(self):
        cycle, display = build_cycle(results=[usage(fetch_error="token_expired")])

        cycle.run_once()

        assert display.states[-1].taskbar_text == "token expired"
        assert display.states[-1].icon_color == "grey"


class TestErrorContainment:
    """A poll cycle must outlive any single failure.

    The display half runs inside pystray's message loop; an escaping exception
    there ends the loop permanently and freezes the tray on its last state with
    nothing but one log line to show for it.
    """

    def test_a_fetch_that_raises_still_displays_an_internal_error(self, caplog):
        cycle, display = build_cycle(results=[RuntimeError("fetcher blew up")])

        with caplog.at_level(logging.ERROR):
            cycle.run_once()

        assert display.states[-1].taskbar_text == "error"
        assert display.states[-1].icon_color == "grey"

    def test_a_fetch_that_raises_does_not_propagate(self):
        cycle, _display = build_cycle(results=[RuntimeError("fetcher blew up")])

        cycle.run_once()

    def test_a_display_that_raises_does_not_propagate(self, caplog):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle(results=[usage(utilization=20.0)], display=display)

        with caplog.at_level(logging.ERROR):
            cycle.run_once()

        assert "display" in caplog.text

    def test_a_notification_that_raises_does_not_propagate(self, caplog):
        display = _RecordingDisplay(notify_raises=OSError("Shell_NotifyIcon failed"))
        notifier = ThresholdNotifier()
        cycle, _display = build_cycle(
            results=[usage(utilization=40.0), usage(utilization=51.0)],
            display=display,
            notifier=notifier,
        )

        with caplog.at_level(logging.ERROR):
            cycle.run_once()
            cycle.run_once()

        assert display.notifications

    def test_a_failed_display_does_not_stop_the_next_cycle(self):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle(
            results=[usage(utilization=20.0), usage(utilization=30.0)],
            display=display,
        )

        cycle.run_once()
        cycle.run_once()

        assert len(display.states) == 2

    def test_refresh_display_survives_a_failing_display(self, caplog):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle(results=[usage(utilization=20.0)], display=display)

        cycle.run_once()
        with caplog.at_level(logging.ERROR):
            cycle.refresh_display()

        assert len(display.states) == 2


class TestRefreshDisplay:
    """Between fetches the relative times keep ticking, so the state is re-rendered."""

    def test_refresh_redisplays_the_latest_result(self):
        cycle, display = build_cycle(results=[usage(utilization=20.0)])
        cycle.run_once()

        cycle.refresh_display()

        assert display.states[-1].taskbar_text == "80% (2h 0m)"
        assert len(display.states) == 2

    def test_refresh_re_reads_the_clock_so_freshness_advances(self):
        display = _RecordingDisplay()
        clock = iter([NOW, NOW + timedelta(seconds=30)])
        cycle = PollCycle(
            Config(),
            fetch=lambda: usage(utilization=20.0, fetched_at=NOW),
            display=display,
            notifier=ThresholdNotifier(),
            nudger=_RecordingNudger(),
            now=lambda: next(clock),
        )

        cycle.run_once()
        cycle.refresh_display()

        assert display.states[0].menu_status_label == "Updated 0s ago"
        assert display.states[1].menu_status_label == "Updated 30s ago"

    def test_refresh_before_any_fetch_shows_nothing(self):
        cycle, display = build_cycle(results=[])

        cycle.refresh_display()

        assert display.states == []


class TestLastGoodUsage:
    """A rate limit does not mean the numbers are wrong, only unrefreshed."""

    def test_a_rate_limit_keeps_showing_the_remembered_usage(self):
        cycle, display = build_cycle(
            results=[
                usage(utilization=25.0),
                usage(fetch_error="rate_limited", status_code=429),
            ]
        )

        cycle.run_once()
        cycle.run_once()

        assert display.states[-1].taskbar_text == "75% (2h 0m)"
        assert display.states[-1].icon_color == "green"

    def test_a_rate_limit_without_any_previous_success_goes_grey(self):
        cycle, display = build_cycle(
            results=[usage(fetch_error="rate_limited", status_code=429)]
        )

        cycle.run_once()

        assert display.states[-1].icon_color == "grey"

    def test_a_fetch_error_does_not_overwrite_the_remembered_usage(self):
        cycle, display = build_cycle(
            results=[
                usage(utilization=25.0),
                usage(fetch_error="offline", status_code=None),
                usage(fetch_error="rate_limited", status_code=429),
            ]
        )

        cycle.run_once()
        cycle.run_once()
        cycle.run_once()

        assert display.states[-1].taskbar_text == "75% (2h 0m)"

    def test_a_success_without_a_usage_window_is_not_remembered(self):
        cycle, display = build_cycle(
            results=[
                usage(),  # a 200 with no five_hour window
                usage(fetch_error="rate_limited", status_code=429),
            ]
        )

        cycle.run_once()
        cycle.run_once()

        assert display.states[-1].icon_color == "grey"


class TestNotifications:
    def test_a_threshold_crossing_reaches_the_display(self):
        cycle, display = build_cycle(
            results=[usage(utilization=40.0), usage(utilization=51.0)]
        )

        cycle.run_once()
        cycle.run_once()

        assert display.notifications == [
            ("Claude usage below 50%", "5h usage has 49% remaining.")
        ]

    def test_the_first_observation_does_not_notify(self):
        cycle, display = build_cycle(results=[usage(utilization=95.0)])

        cycle.run_once()

        assert display.notifications == []

    def test_a_fetch_error_produces_no_notifications(self):
        cycle, display = build_cycle(
            results=[usage(utilization=40.0), usage(fetch_error="offline")]
        )

        cycle.run_once()
        cycle.run_once()

        assert display.notifications == []


class TestSessionNudge:
    def test_every_fetch_is_offered_to_the_nudger(self):
        nudger = _RecordingNudger()
        cycle, _display = build_cycle(
            results=[usage(utilization=0.0), usage(fetch_error="token_expired")],
            nudger=nudger,
        )

        cycle.run_once()
        cycle.run_once()

        assert len(nudger.seen) == 2

    def test_an_exhausted_nudger_changes_the_wording(self):
        cycle, display = build_cycle(
            results=[usage(fetch_error="token_expired")],
            nudger=_RecordingNudger(exhausted=True),
        )

        cycle.run_once()

        assert display.states[-1].taskbar_text == "sign in"

    def test_the_breaker_is_re_read_on_every_render(self):
        """The nudge runs on its own thread, so the breaker can trip mid-wait."""
        nudger = _RecordingNudger(exhausted=False)
        cycle, display = build_cycle(
            results=[usage(fetch_error="token_expired")], nudger=nudger
        )
        cycle.run_once()

        nudger.exhausted = True
        cycle.refresh_display()

        assert display.states[-1].taskbar_text == "sign in"


# ===========================================================================
# run_until — the loop around run_once.
# ===========================================================================


class TestRunUntil:
    def _waits(self, cycle, shutdown, manual, *, stop_after: int):
        """Wait that lets the loop turn a fixed number of times."""
        calls: list[tuple[int, object]] = []

        def wait(seconds: int, refresh) -> bool:
            calls.append((seconds, refresh))
            if len(calls) >= stop_after:
                shutdown.set()
            return False

        return calls, wait

    def test_the_loop_runs_until_shutdown_is_requested(self):
        cycle, display = build_cycle(
            results=[usage(utilization=20.0) for _ in range(3)]
        )
        shutdown, manual = threading.Event(), threading.Event()
        _calls, wait = self._waits(cycle, shutdown, manual, stop_after=3)

        cycle.run_until(shutdown, manual, wait)

        assert len(display.states) == 3

    def test_the_loop_does_not_start_when_shutdown_is_already_set(self):
        cycle, display = build_cycle(results=[])
        shutdown = threading.Event()
        shutdown.set()

        cycle.run_until(shutdown, threading.Event(), lambda seconds, refresh: False)

        assert display.states == []

    def test_the_manual_refresh_flag_is_cleared_between_cycles(self):
        cycle, _display = build_cycle(results=[usage(utilization=20.0)])
        shutdown, manual = threading.Event(), threading.Event()
        manual.set()
        _calls, wait = self._waits(cycle, shutdown, manual, stop_after=1)

        cycle.run_until(shutdown, manual, wait)

        assert not manual.is_set()

    def test_the_wait_is_given_the_current_interval(self):
        cycle, _display = build_cycle(
            results=[usage(fetch_error="rate_limited", status_code=429)],
            config=Config(),
        )
        shutdown, manual = threading.Event(), threading.Event()
        calls, wait = self._waits(cycle, shutdown, manual, stop_after=1)

        cycle.run_until(shutdown, manual, wait)

        # 60s baseline doubled by the rate limit.
        assert calls[0][0] == 120

    def test_shutdown_during_a_cycle_skips_the_wait_entirely(self):
        shutdown, manual = threading.Event(), threading.Event()
        display = _RecordingDisplay()
        waits: list[int] = []

        def fetch() -> AnthropicUsageData:
            shutdown.set()
            return usage(utilization=20.0)

        cycle = PollCycle(
            Config(),
            fetch=fetch,
            display=display,
            notifier=ThresholdNotifier(),
            nudger=_RecordingNudger(),
            now=lambda: NOW,
        )

        cycle.run_until(shutdown, manual, lambda seconds, refresh: waits.append(seconds))

        assert len(display.states) == 1
        assert waits == []


# ===========================================================================
# _next_poll_interval_seconds — the adaptive cadence.
# ===========================================================================


class TestIntervalSeconds:
    """The cycle reports its own cadence, so the waiter never has to compute it."""

    def test_it_starts_at_the_configured_baseline(self):
        cycle, _display = build_cycle(results=[])

        assert cycle.interval_seconds == 60

    def test_it_backs_off_after_a_rate_limit(self):
        cycle, _display = build_cycle(
            results=[usage(fetch_error="rate_limited", status_code=429)]
        )

        cycle.run_once()

        assert cycle.interval_seconds == 120

    def test_a_fetch_that_raises_leaves_the_cadence_alone(self):
        cycle, _display = build_cycle(results=[RuntimeError("boom")])

        cycle.run_once()

        assert cycle.interval_seconds == 60


class TestNextPollInterval:
    def test_doubles_after_a_rate_limit(self):
        data = usage(fetch_error="rate_limited", status_code=429)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 120

    def test_backoff_is_capped(self):
        data = usage(fetch_error="rate_limited", status_code=429)

        assert _next_poll_interval_seconds(400, data, baseline_seconds=60) == 600
        assert _next_poll_interval_seconds(600, data, baseline_seconds=60) == 600

    def test_a_server_retry_after_wins_over_the_backoff(self):
        data = usage(fetch_error="rate_limited", status_code=429, retry_after_seconds=224)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 224

    def test_a_short_retry_after_is_floored_at_the_baseline(self):
        data = usage(fetch_error="rate_limited", status_code=429, retry_after_seconds=30)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 60

    def test_falls_back_to_the_backoff_without_a_retry_after(self):
        data = usage(fetch_error="rate_limited", status_code=429, retry_after_seconds=None)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 120

    def test_recovers_five_seconds_at_a_time_after_a_success(self):
        assert _next_poll_interval_seconds(90, usage(), baseline_seconds=60) == 85

    def test_never_drops_below_the_baseline(self):
        assert _next_poll_interval_seconds(60, usage(), baseline_seconds=60) == 60

    def test_clamps_to_the_baseline_when_less_than_one_step_above_it(self):
        assert _next_poll_interval_seconds(63, usage(), baseline_seconds=60) == 60

    def test_stays_the_same_after_a_non_rate_limit_error(self):
        data = usage(fetch_error="token_expired", status_code=401)

        assert _next_poll_interval_seconds(90, data, baseline_seconds=60) == 90

    def test_stays_the_same_after_an_offline_error_without_a_status(self):
        data = usage(fetch_error="offline", status_code=None)

        assert _next_poll_interval_seconds(90, data, baseline_seconds=60) == 90
