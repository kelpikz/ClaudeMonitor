"""Run the fetch → process → display cycle that drives the whole application.

The cycle owns what the loop has to remember between polls: the last successful
fetch, so a rate limit can keep showing real usage, and the adaptive interval,
so a healthy run recovers its cadence.

Both halves of a cycle are contained. Neither a failing fetch nor a failing
display may end the loop, because a loop that ends leaves the tray frozen on
stale numbers with nothing but one log line to explain it — and a frozen tray
looks identical to a working one.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

from . import fetcher, processor
from .config import Config
from .models import AnthropicUsageData, DisplayState
from .notifications import ThresholdNotifier, UsageNotification

log = logging.getLogger(__name__)

_POLL_INTERVAL_RECOVERY_STEP_SECONDS = 5
_POLL_INTERVAL_BACKOFF_FACTOR = 2
_POLL_INTERVAL_CAP_SECONDS = 600


class Display(Protocol):
    """The surfaces one processed state is pushed to."""

    def apply(self, state: DisplayState) -> None: ...
    def notify(self, title: str, message: str) -> None: ...


class SessionNudge(Protocol):
    """The CLI session nudge, as much of it as the cycle needs to see."""

    @property
    def exhausted(self) -> bool: ...
    def maybe_nudge(self, data: AnthropicUsageData) -> bool: ...


# Wait for the next poll, refreshing the display meanwhile; returns whether a
# manual refresh cut the wait short.
Wait = Callable[[int, Callable[[], None]], object]


def _is_successful_fetch(data: AnthropicUsageData) -> bool:
    """Return whether a fetch completed successfully enough to update freshness."""
    return data.fetch_error is None and data.status_code == 200


def _next_poll_interval_seconds(
    current_interval_seconds: int,
    data: AnthropicUsageData,
    *,
    baseline_seconds: int,
) -> int:
    """Honor a server Retry-After on rate-limit, else double the interval
    (capped); recover toward the configured baseline after a success."""
    if data.status_code == 429 or data.fetch_error == "rate_limited":
        if data.retry_after_seconds is not None:
            return max(baseline_seconds, data.retry_after_seconds)
        return min(
            _POLL_INTERVAL_CAP_SECONDS,
            current_interval_seconds * _POLL_INTERVAL_BACKOFF_FACTOR,
        )
    if _is_successful_fetch(data):
        return max(
            baseline_seconds,
            current_interval_seconds - _POLL_INTERVAL_RECOVERY_STEP_SECONDS,
        )
    return current_interval_seconds


def _now_utc() -> datetime:
    """Return the current time, injected so tests can freeze the countdowns."""
    return datetime.now(timezone.utc)


class PollCycle:
    """Fetch usage, process it, and show it — repeatedly, without ever raising."""

    def __init__(
        self,
        config: Config,
        *,
        display: Display,
        fetch: Callable[[], AnthropicUsageData] = fetcher.fetch,
        notifier: ThresholdNotifier | None = None,
        nudger: SessionNudge | None = None,
        now: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._config = config
        self._display = display
        self._fetch = fetch
        self._notifier = notifier if notifier is not None else ThresholdNotifier()
        self._nudger = nudger
        self._now = now
        self._interval_seconds = config.polling.interval_seconds
        self._latest: AnthropicUsageData | None = None
        self._last_good: AnthropicUsageData | None = None
        self._failed = False

    @property
    def interval_seconds(self) -> int:
        """Return how long to wait before the next fetch."""
        return self._interval_seconds

    def run_once(self) -> DisplayState:
        """Run one whole cycle and return the state it showed."""
        notifications = self._poll()
        state = self._render()
        self._show(state, notifications)
        return state

    def refresh_display(self) -> None:
        """Redraw the latest result so its relative times keep ticking."""
        if self._latest is None and not self._failed:
            return
        self._show(self._render(), ())

    def run_until(
        self,
        shutdown_requested: threading.Event,
        manual_refresh: threading.Event,
        wait: Wait,
    ) -> None:
        """Poll until shutdown is requested, waiting between cycles."""
        while not shutdown_requested.is_set():
            self.run_once()
            manual_refresh.clear()
            if shutdown_requested.is_set():
                return
            wait(self._interval_seconds, self.refresh_display)

    def _poll(self) -> list[UsageNotification]:
        """Fetch once and fold the result into the cycle's state."""
        try:
            data = self._fetch()
            notifications = self._notifier.check(data)
            if self._nudger is not None:
                self._nudger.maybe_nudge(data)
            if data.fetch_error is None and data.five_hour is not None:
                # Remember the most recent real usage so a later rate limit can
                # keep showing it instead of going grey.
                self._last_good = data
            self._interval_seconds = _next_poll_interval_seconds(
                self._interval_seconds,
                data,
                baseline_seconds=self._config.polling.interval_seconds,
            )
            self._latest = data
            self._failed = False
            return notifications
        except Exception:
            log.exception("unhandled error in poll cycle")
            self._failed = True
            return []

    def _render(self) -> DisplayState:
        """Turn what the cycle currently knows into one display state.

        The nudge's breaker is read here rather than at fetch time: the nudge
        runs on its own thread, so it can trip mid-wait and the tooltip should
        say so without waiting for the next fetch.
        """
        if self._failed or self._latest is None:
            return processor.internal_error_state(now=self._now())
        return processor.getDataToDisplay(
            self._latest,
            now=self._now(),
            config=self._config,
            last_good=self._last_good,
            session_refresh_exhausted=(
                self._nudger is not None and self._nudger.exhausted
            ),
        )

    def _show(
        self,
        state: DisplayState,
        notifications: Iterable[UsageNotification],
    ) -> None:
        """Push one state to the display and raise any pending notifications.

        A surface that fails — pystray rejecting an icon update, Windows
        refusing a notification — must not end the loop that feeds it.
        """
        try:
            self._display.apply(state)
            for notification in notifications:
                self._display.notify(notification.title, notification.message)
        except Exception:
            log.exception("unable to update the display")
