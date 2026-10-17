"""Coverage for the poll cycle — the loop that drives the whole application.

The cycle tests start at what a provider's fetch returned and finish at what
the display is asked to show, through the real ``ProviderPoller`` and the real
processor, so the composition is exercised and not only the helpers it calls.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from claudemonitor import poll_cycle
from claudemonitor.config import CLAUDE_SETTINGS, CODEX_SETTINGS, CodexConfig, Config
from claudemonitor.models import CLAUDE, CODEX, DisplayState, ProviderUsageData, UsageWindow
from claudemonitor.poll_cycle import (
    PollCycle,
    ProviderPoller,
    active_pollers,
    _next_poll_interval_seconds,
    _poll_provider,
    _provider_display,
    _shared_poll_interval,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

def usage(
    *,
    utilization: float | None = None,
    fetch_error: str | None = None,
    status_code: int | None = 200,
    retry_after_seconds: int | None = None,
) -> ProviderUsageData:
    """Build one fetch result, with a 5h window only when a utilization is given."""
    five_hour = (
        UsageWindow(utilization=utilization, resets_at=NOW + timedelta(hours=2))
        if utilization is not None
        else None
    )
    return ProviderUsageData(
        five_hour=five_hour,
        fetch_error=fetch_error,
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        fetched_at=NOW,
    )


class _RecordingDisplay:
    """Capture what the tray and taskbar would be asked to show."""

    def __init__(
        self,
        *,
        apply_raises: Exception | None = None,
        notify_raises: Exception | None = None,
    ) -> None:
        self.shown: list[list[DisplayState]] = []
        self.notifications: list[tuple[str, str]] = []
        self._apply_raises = apply_raises
        self._notify_raises = notify_raises

    @property
    def last(self) -> list[DisplayState]:
        return self.shown[-1]

    def apply(self, states: list[DisplayState]) -> None:
        self.shown.append(states)
        if self._apply_raises is not None:
            raise self._apply_raises

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))
        if self._notify_raises is not None:
            raise self._notify_raises


class _RecordingNudger:
    """Record every fetch offered to the CLI nudge, and report a breaker."""

    def __init__(self, exhausted: bool = False) -> None:
        self.seen: list[ProviderUsageData] = []
        self.exhausted = exhausted

    def maybe_nudge(self, data: ProviderUsageData) -> bool:
        self.seen.append(data)
        return False


def _scripted(provider, results: list):
    """Give a provider a fetch that replays a list of results or errors."""
    pending = iter(results)

    def fetch() -> ProviderUsageData:
        outcome = next(pending)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return replace(provider, fetch=fetch)


def build_cycle(
    results: list,
    *,
    display: _RecordingDisplay | None = None,
    nudger: _RecordingNudger | None = None,
    config: Config | None = None,
    now=lambda: NOW,
) -> tuple[PollCycle, _RecordingDisplay]:
    """Build a Claude-only cycle whose fetches replay a scripted list."""
    config = config or Config()
    display = display or _RecordingDisplay()
    poller = ProviderPoller(
        _scripted(CLAUDE, results), config, nudger or _RecordingNudger()
    )
    return PollCycle([poller], config, display, now=now), display


# ===========================================================================
# run_once — one whole fetch -> process -> display cycle.
# ===========================================================================


class TestRunOnceHappyPath:
    def test_successful_usage_reaches_the_display(self):
        cycle, display = build_cycle([usage(utilization=20.0)])

        cycle.run_once()

        assert display.last[0].taskbar_text == "80% (2h 0m)"
        assert display.last[0].icon_color == "green"

    def test_run_once_returns_the_states_it_displayed(self):
        cycle, display = build_cycle([usage(utilization=20.0)])

        assert cycle.run_once() is display.last

    def test_each_fetch_error_reaches_the_display_as_its_own_message(self):
        cycle, display = build_cycle([usage(fetch_error="token_expired", status_code=401)])

        cycle.run_once()

        assert display.last[0].taskbar_text == "token expired"
        assert display.last[0].icon_color == "grey"

    def test_both_providers_are_shown_in_order(self):
        config = Config(codex=CodexConfig(enabled=True))
        pollers = [
            ProviderPoller(_scripted(CLAUDE, [usage(utilization=20.0)]), config, _RecordingNudger()),
            ProviderPoller(_scripted(CODEX, [usage(utilization=40.0)]), config, _RecordingNudger()),
        ]
        display = _RecordingDisplay()

        PollCycle(pollers, config, display, now=lambda: NOW).run_once()

        assert [state.provider.key for state in display.last] == ["claude", "codex"]

    def test_an_untracked_provider_is_neither_polled_nor_shown(self):
        # Codex is off by default; a fetch it is never asked for cannot fail.
        config = Config()
        pollers = [
            ProviderPoller(_scripted(CLAUDE, [usage(utilization=20.0)]), config, _RecordingNudger()),
            ProviderPoller(_scripted(CODEX, []), config, _RecordingNudger()),
        ]
        display = _RecordingDisplay()

        PollCycle(pollers, config, display, now=lambda: NOW).run_once()

        assert [state.provider.key for state in display.last] == ["claude"]


class TestErrorContainment:
    """A poll cycle must outlive any single failure.

    The loop runs on pystray's setup thread. An exception that escapes it ends
    the loop for good and leaves the tray frozen on its last numbers, which
    looks exactly like a tray that is working.
    """

    def test_a_fetch_that_raises_shows_that_providers_error_tile(self, caplog):
        cycle, display = build_cycle([RuntimeError("fetcher blew up")])

        with caplog.at_level(logging.ERROR):
            cycle.run_once()

        assert display.last[0].provider.key == "claude"

    def test_a_tick_that_raises_shows_an_internal_error(self, monkeypatch, caplog):
        cycle, display = build_cycle([usage(utilization=20.0)])

        def broken(pollers, config):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(poll_cycle, "active_pollers", broken)
        with caplog.at_level(logging.ERROR):
            cycle.run_once()

        assert display.last[0].taskbar_text == "error"
        assert "poll loop" in caplog.text

    def test_a_tick_that_raises_waits_the_configured_interval(self, monkeypatch):
        cycle, _display = build_cycle([])

        def broken(pollers, config):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(poll_cycle, "active_pollers", broken)
        cycle.run_once()

        assert cycle.interval_seconds == 60

    def test_a_display_that_raises_does_not_propagate(self, caplog):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle([usage(utilization=20.0)], display=display)

        with caplog.at_level(logging.ERROR):
            cycle.run_once()

        assert "display" in caplog.text

    def test_a_notification_that_raises_does_not_propagate(self, caplog):
        display = _RecordingDisplay(notify_raises=OSError("Shell_NotifyIcon failed"))
        cycle, _display = build_cycle(
            [usage(utilization=40.0), usage(utilization=51.0)], display=display
        )

        with caplog.at_level(logging.ERROR):
            cycle.run_once()
            cycle.run_once()

        assert display.notifications

    def test_a_failed_display_does_not_stop_the_next_cycle(self):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle(
            [usage(utilization=20.0), usage(utilization=30.0)], display=display
        )

        cycle.run_once()
        cycle.run_once()

        assert len(display.shown) == 2

    def test_refresh_display_survives_a_failing_display(self, caplog):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle([usage(utilization=20.0)], display=display)

        cycle.run_once()
        with caplog.at_level(logging.ERROR):
            cycle.refresh_display()

        assert len(display.shown) == 2


class TestRefreshDisplay:
    """Between fetches the relative times keep ticking, so the state is redrawn."""

    def test_refresh_redisplays_the_latest_result(self):
        cycle, display = build_cycle([usage(utilization=20.0)])
        cycle.run_once()

        cycle.refresh_display()

        assert display.last[0].taskbar_text == "80% (2h 0m)"
        assert len(display.shown) == 2

    def test_refresh_re_reads_the_clock_so_freshness_advances(self):
        clock = iter([NOW, NOW + timedelta(seconds=30)])
        cycle, display = build_cycle([usage(utilization=20.0)], now=lambda: next(clock))

        cycle.run_once()
        cycle.refresh_display()

        assert "0s ago" in display.shown[0][0].menu_status_label
        assert "30s ago" in display.shown[1][0].menu_status_label

    def test_refresh_before_any_poll_shows_nothing(self):
        cycle, display = build_cycle([])

        cycle.refresh_display()

        assert display.shown == []

    def test_the_nudge_breaker_is_re_read_on_every_redraw(self):
        # The nudge runs on its own thread, so the breaker can trip mid-wait.
        nudger = _RecordingNudger(exhausted=False)
        cycle, display = build_cycle(
            [usage(fetch_error="token_expired", status_code=401)], nudger=nudger
        )
        cycle.run_once()
        before = display.last[0].tooltip

        nudger.exhausted = True
        cycle.refresh_display()

        assert display.last[0].tooltip != before


class TestLastGoodUsage:
    """A rate limit does not mean the numbers are wrong, only unrefreshed."""

    def test_a_rate_limit_keeps_showing_the_remembered_usage(self):
        cycle, display = build_cycle(
            [usage(utilization=25.0), usage(fetch_error="rate_limited", status_code=429)]
        )

        cycle.run_once()
        cycle.run_once()

        assert display.last[0].taskbar_text.startswith("75%")

    def test_a_rate_limit_without_any_previous_success_goes_grey(self):
        cycle, display = build_cycle([usage(fetch_error="rate_limited", status_code=429)])

        cycle.run_once()

        assert display.last[0].icon_color == "grey"


class TestNotifications:
    def test_a_threshold_crossing_reaches_the_display(self):
        cycle, display = build_cycle([usage(utilization=40.0), usage(utilization=51.0)])

        cycle.run_once()
        cycle.run_once()

        assert [title for title, _message in display.notifications] == [
            "Claude usage below 50%"
        ]

    def test_the_first_observation_does_not_notify(self):
        cycle, display = build_cycle([usage(utilization=95.0)])

        cycle.run_once()

        assert display.notifications == []


class TestSessionNudge:
    def test_every_fetch_is_offered_to_the_nudger(self):
        nudger = _RecordingNudger()
        cycle, _display = build_cycle(
            [usage(utilization=0.0), usage(fetch_error="token_expired", status_code=401)],
            nudger=nudger,
        )

        cycle.run_once()
        cycle.run_once()

        assert len(nudger.seen) == 2


# ===========================================================================
# run_until — the loop around run_once.
# ===========================================================================


class TestRunUntil:
    def _stopping_wait(self, shutdown: threading.Event, *, stop_after: int):
        """A wait that lets the loop turn a fixed number of times."""
        calls: list[int] = []

        def wait(seconds: int, refresh) -> bool:
            calls.append(seconds)
            if len(calls) >= stop_after:
                shutdown.set()
            return False

        return calls, wait

    def test_the_loop_runs_until_shutdown_is_requested(self):
        cycle, display = build_cycle([usage(utilization=20.0) for _ in range(3)])
        shutdown = threading.Event()
        _calls, wait = self._stopping_wait(shutdown, stop_after=3)

        cycle.run_until(shutdown, threading.Event(), wait)

        assert len(display.shown) == 3

    def test_the_loop_does_not_start_when_shutdown_is_already_set(self):
        cycle, display = build_cycle([])
        shutdown = threading.Event()
        shutdown.set()

        cycle.run_until(shutdown, threading.Event(), lambda seconds, refresh: False)

        assert display.shown == []

    def test_the_manual_refresh_flag_is_cleared_between_cycles(self):
        cycle, _display = build_cycle([usage(utilization=20.0)])
        shutdown, manual = threading.Event(), threading.Event()
        manual.set()
        _calls, wait = self._stopping_wait(shutdown, stop_after=1)

        cycle.run_until(shutdown, manual, wait)

        assert not manual.is_set()

    def test_the_wait_is_given_the_current_interval(self):
        cycle, _display = build_cycle([usage(fetch_error="rate_limited", status_code=429)])
        shutdown = threading.Event()
        calls, wait = self._stopping_wait(shutdown, stop_after=1)

        cycle.run_until(shutdown, threading.Event(), wait)

        # 60s baseline doubled by the rate limit.
        assert calls == [120]

    def test_the_wait_redraws_through_the_cycle(self):
        cycle, display = build_cycle([usage(utilization=20.0)])
        shutdown = threading.Event()

        def wait(seconds: int, refresh) -> bool:
            refresh()
            shutdown.set()
            return False

        cycle.run_until(shutdown, threading.Event(), wait)

        assert len(display.shown) == 2

    def test_shutdown_during_a_cycle_skips_the_wait_entirely(self):
        shutdown = threading.Event()
        waits: list[int] = []

        def fetch() -> ProviderUsageData:
            shutdown.set()
            return usage(utilization=20.0)

        config = Config()
        poller = ProviderPoller(replace(CLAUDE, fetch=fetch), config, _RecordingNudger())
        display = _RecordingDisplay()
        cycle = PollCycle([poller], config, display, now=lambda: NOW)

        cycle.run_until(shutdown, threading.Event(), lambda s, refresh: waits.append(s))

        assert len(display.shown) == 1
        assert waits == []

    def test_a_display_that_always_raises_does_not_end_the_loop(self):
        display = _RecordingDisplay(apply_raises=ValueError("string too long"))
        cycle, _display = build_cycle(
            [usage(utilization=20.0) for _ in range(3)], display=display
        )
        shutdown = threading.Event()
        _calls, wait = self._stopping_wait(shutdown, stop_after=3)

        cycle.run_until(shutdown, threading.Event(), wait)

        assert len(display.shown) == 3


class TestIntervalSeconds:
    """The cycle reports its own cadence, so the waiter never has to compute it."""

    def test_it_starts_at_the_configured_baseline(self):
        cycle, _display = build_cycle([])

        assert cycle.interval_seconds == 60

    def test_it_backs_off_after_a_rate_limit(self):
        cycle, _display = build_cycle([usage(fetch_error="rate_limited", status_code=429)])

        cycle.run_once()

        assert cycle.interval_seconds == 120

    def test_with_nothing_tracked_it_still_waits(self):
        config = Config()
        CLAUDE_SETTINGS.tracking.write(config, False)
        cycle = PollCycle([], config, _RecordingDisplay(), now=lambda: NOW)

        cycle.run_once()

        assert cycle.interval_seconds > 0


# ===========================================================================
# The helpers the cycle is built from.
# ===========================================================================


class TestNextPollInterval:
    def test_doubles_after_a_rate_limit(self):
        data = usage(fetch_error="rate_limited", status_code=429)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 120

    def test_backoff_is_capped(self):
        data = usage(fetch_error="rate_limited", status_code=429)

        assert _next_poll_interval_seconds(400, data, baseline_seconds=60) == 600
        assert _next_poll_interval_seconds(600, data, baseline_seconds=60) == 600

    def test_honors_retry_after_on_rate_limit(self):
        data = usage(fetch_error="rate_limited", status_code=429, retry_after_seconds=224)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 224

    def test_retry_after_is_floored_at_baseline(self):
        data = usage(fetch_error="rate_limited", status_code=429, retry_after_seconds=30)

        assert _next_poll_interval_seconds(60, data, baseline_seconds=60) == 60

    def test_decreases_by_five_seconds_after_success(self):
        assert _next_poll_interval_seconds(90, usage(), baseline_seconds=60) == 85

    def test_clamps_to_baseline_when_less_than_step_above_it(self):
        assert _next_poll_interval_seconds(63, usage(), baseline_seconds=60) == 60

    def test_stays_same_after_non_rate_limit_error(self):
        data = usage(fetch_error="token_expired", status_code=401)

        assert _next_poll_interval_seconds(90, data, baseline_seconds=60) == 90


class _TrackedPoller:
    """A poller that is only ever asked which provider it is for."""

    def __init__(self, provider):
        self.provider = provider


class TestActiveProviders:
    """The poll loop only works on providers the user is actually tracking."""

    def test_only_claude_is_polled_by_default(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        assert active_pollers([claude, codex], Config()) == [claude]

    def test_both_providers_are_polled_when_codex_is_on(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)
        config = Config(codex=CodexConfig(enabled=True))

        assert active_pollers([claude, codex], config) == [claude, codex]

    def test_claude_is_dropped_when_switched_off(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        config = Config(codex=CodexConfig(enabled=True))
        CLAUDE_SETTINGS.tracking.write(config, False)

        assert active_pollers([claude, codex], config) == [codex]

    def test_nothing_is_polled_when_both_are_switched_off(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        config = Config()
        CLAUDE_SETTINGS.tracking.write(config, False)
        CODEX_SETTINGS.tracking.write(config, False)

        assert active_pollers([claude, codex], config) == []


class TestSharedPollInterval:
    """One loop serves both providers, so the slower of the two sets the pace."""

    def test_the_longer_interval_wins(self):
        assert _shared_poll_interval([60, 240]) == 240

    def test_no_providers_falls_back_to_a_safe_default(self):
        assert _shared_poll_interval([]) > 0


class TestProviderFailureIsolation:
    """One provider misbehaving must never blank the other one's numbers."""

    class _Poller:
        def __init__(self, provider, *, raises: Exception | None = None):
            self.provider = provider
            self._raises = raises

        def poll(self):
            if self._raises is not None:
                raise self._raises
            return ["notification"]

        def display(self, now):
            if self._raises is not None:
                raise self._raises
            return DisplayState(
                provider=self.provider,
                icon_color="green",
                tooltip="fine",
                menu_status_label="Updated 0s ago",
                taskbar_text="80%",
                tray_text="80%",
            )

    def test_a_failing_poll_returns_no_notifications(self, caplog):
        poller = self._Poller(CODEX, raises=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            assert _poll_provider(poller) == []

        assert "Codex" in caplog.text

    def test_a_failing_display_still_yields_a_state_for_that_provider(self, caplog):
        poller = self._Poller(CODEX, raises=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            state = _provider_display(poller, NOW)

        assert state.provider is CODEX
        assert state.icon_color == "grey"

    def test_one_broken_provider_leaves_the_other_intact(self):
        healthy = self._Poller(CLAUDE)
        broken = self._Poller(CODEX, raises=RuntimeError("boom"))

        states = [_provider_display(p, NOW) for p in (healthy, broken)]

        assert [s.icon_color for s in states] == ["green", "grey"]
