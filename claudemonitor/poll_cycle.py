"""Run the fetch → process → display cycle that drives the whole application.

Each ``ProviderPoller`` owns one provider's state: its backoff, its last good
reading, its notifications. The ``PollCycle`` owns the loop around them: which
providers are tracked this tick, how long to wait, and what to show.

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

from . import cli_refresher, processor
from .config import Config
from .models import PROVIDERS, DisplayState, Provider, ProviderUsageData
from .notifications import ThresholdNotifier, UsageNotification

log = logging.getLogger(__name__)

_POLL_INTERVAL_RECOVERY_STEP_SECONDS = 5
_POLL_INTERVAL_BACKOFF_FACTOR = 2
_POLL_INTERVAL_CAP_SECONDS = 600

# Used when the user has switched every provider off. It keeps the loop's
# wait from collapsing to zero while it waits for one to be switched on.
_FALLBACK_POLL_INTERVAL_SECONDS = 60


class Display(Protocol):
    """The surfaces every provider's display state is pushed to."""

    def apply(self, states: list[DisplayState]) -> None: ...
    def notify(self, title: str, message: str) -> None: ...


# Wait for the next poll, redrawing meanwhile; returns whether a manual
# refresh cut the wait short.
Wait = Callable[[int, Callable[[], None]], object]


def _now_utc() -> datetime:
    """Return the current time, injected so tests can freeze the countdowns."""
    return datetime.now(timezone.utc)


def is_tracked(provider: Provider, config: Config) -> bool:
    """Return whether the user is tracking this provider at all."""
    return bool(provider.settings.tracking.read(config))


def _next_poll_interval_seconds(
    current_interval_seconds: int,
    data: ProviderUsageData,
    *,
    baseline_seconds: int,
) -> int:
    """Honor a server Retry-After on rate-limit, else double the interval (capped); recover toward the configured baseline after a success."""
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


def _is_successful_fetch(data: ProviderUsageData) -> bool:
    """Return whether a fetch completed successfully enough to update freshness."""
    return data.fetch_error is None and data.status_code == 200


def active_pollers(pollers: list["ProviderPoller"], config: Config) -> list:
    """Return the pollers to run this tick, in the order they are displayed."""
    return [poller for poller in pollers if is_tracked(poller.provider, config)]


def _shared_poll_interval(intervals: list[int]) -> int:
    """Return one wait that satisfies every provider.

    A single loop serves both, so the provider asking for the longest gap —
    a rate-limited one, typically — sets the pace for the tick.
    """
    return max(intervals, default=_FALLBACK_POLL_INTERVAL_SECONDS)


class ProviderPoller:
    """Own one provider's fetching, its backoff, and its last good reading.

    Each provider fails independently — Codex can be rate-limited while
    Claude is fine — so every piece of per-provider state lives in here
    rather than in the loop that drives them.
    """

    def __init__(
        self,
        provider: Provider,
        config: Config,
        nudger: cli_refresher.SessionNudger,
    ) -> None:
        self.provider = provider
        self._config = config
        self._nudger = nudger
        self._notifier = ThresholdNotifier(provider_label=provider.label)
        self.interval_seconds = config.polling.interval_seconds
        # Remember the most recent successful fetch so a later rate-limit
        # (429) can keep showing real usage instead of a grey "offline" icon.
        self._last_good: ProviderUsageData | None = None
        self._latest = ProviderUsageData(
            fetch_error="no_data",
            fetched_at=datetime.now(timezone.utc),
        )

    def poll(self) -> list[UsageNotification]:
        """Fetch once, updating freshness, backoff, and the CLI nudge."""
        data = self.provider.fetch()
        self._latest = data
        notifications = self._notifier.check(data)
        self._nudger.maybe_nudge(data)
        if data.fetch_error is None and data.five_hour is not None:
            self._last_good = data
        self.interval_seconds = _next_poll_interval_seconds(
            self.interval_seconds,
            data,
            baseline_seconds=self._config.polling.interval_seconds,
        )
        return notifications

    def display(self, now: datetime) -> DisplayState:
        """Format the most recent fetch for the tray and the taskbar.

        The nudger's ``exhausted`` flag is read per call, not per poll: the
        nudge runs on its own thread, so the breaker can trip mid-wait and
        the tooltip should say so without waiting for the next fetch.
        """
        return processor.getDataToDisplay(
            self._latest,
            now,
            self._config,
            self.provider,
            last_good=self._last_good,
            session_refresh_exhausted=self._nudger.exhausted,
        )


def _poll_provider(poller: "ProviderPoller") -> list[UsageNotification]:
    """Poll one provider, keeping its failure away from the others.

    A fetcher never raises by contract, but a bug anywhere on one provider's
    path must not cost the user the other provider's numbers as well.
    """
    try:
        return poller.poll()
    except Exception:
        log.exception("poll failed for %s", poller.provider.label)
        return []


def _provider_display(poller: "ProviderPoller", now: datetime) -> DisplayState:
    """Format one provider, falling back to its own error tile if that fails."""
    try:
        return poller.display(now)
    except Exception:
        log.exception("display failed for %s", poller.provider.label)
        return processor.internal_error_state(now, poller.provider)


class PollCycle:
    """Poll every tracked provider and show the result — repeatedly, never raising."""

    def __init__(
        self,
        pollers: list[ProviderPoller],
        config: Config,
        display: Display,
        *,
        now: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._pollers = pollers
        self._config = config
        self._display = display
        self._now = now
        self._interval_seconds = config.polling.interval_seconds
        self._active: list[ProviderPoller] = []
        self._polled = False
        self._failed = False

    @property
    def interval_seconds(self) -> int:
        """Return how long to wait before the next poll."""
        return self._interval_seconds

    def run_once(self) -> list[DisplayState]:
        """Run one whole cycle and return the states it showed."""
        notifications = self._poll()
        states = self._render()
        self._show(states, notifications)
        return states

    def refresh_display(self) -> None:
        """Redraw the latest result so its relative times keep ticking."""
        if not self._polled:
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
        """Poll every tracked provider and settle the wait before the next tick."""
        self._polled = True
        try:
            self._active = active_pollers(self._pollers, self._config)
            notifications: list[UsageNotification] = []
            for poller in self._active:
                notifications += _poll_provider(poller)
            self._interval_seconds = _shared_poll_interval(
                [poller.interval_seconds for poller in self._active]
            )
            self._failed = False
            return notifications
        except Exception:
            log.exception("unhandled error in poll loop")
            self._interval_seconds = self._config.polling.interval_seconds
            self._failed = True
            return []

    def _render(self) -> list[DisplayState]:
        """Turn what the cycle currently knows into one state per provider."""
        now = self._now()
        if self._failed:
            return [processor.internal_error_state(now, PROVIDERS[0])]
        return [_provider_display(poller, now) for poller in self._active]

    def _show(
        self,
        states: list[DisplayState],
        notifications: Iterable[UsageNotification],
    ) -> None:
        """Push the states to the display and raise any pending notifications.

        A surface that fails — pystray rejecting an icon update, Windows
        refusing a notification — must not end the loop that feeds it.
        """
        try:
            self._display.apply(states)
            for notification in notifications:
                self._display.notify(notification.title, notification.message)
        except Exception:
            log.exception("unable to update the display")
