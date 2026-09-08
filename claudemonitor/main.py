from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import pystray

from . import autostart, cli_refresher, codex_fetcher, fetcher, processor, tray
from .config import (
    Config,
    load_config,
    save_amber_threshold,
    save_codex_enabled,
    save_poll_interval_seconds,
    save_red_threshold,
    save_session_refresh_cooldown,
    save_session_refresh_enabled,
    save_taskbar_enabled,
)
from .models import CLAUDE, CODEX, DisplayState, Provider, ProviderUsageData
from .notifications import ThresholdNotifier, UsageNotification
from .settings import (
    Number,
    SettingsModel,
    SettingsView,
    SettingsWindowController,
    Switch,
    build_settings,
)
from .taskbar_companion import TaskbarDisplay, create_taskbar_companion
from .win32_settings_window import create_settings_window
from .win32_taskbar_window import enable_per_monitor_dpi_awareness

_ERROR_ALREADY_EXISTS = 183
log = logging.getLogger(__name__)

_POLL_INTERVAL_RECOVERY_STEP_SECONDS = 5
_POLL_INTERVAL_BACKOFF_FACTOR = 2
_POLL_INTERVAL_CAP_SECONDS = 600
_DISPLAY_REFRESH_INTERVAL_SECONDS = 1
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1

# Used only when no provider is being tracked, which cannot normally happen:
# Claude is always on. It keeps the loop's wait from collapsing to zero.
_FALLBACK_POLL_INTERVAL_SECONDS = 60


def _apply_display(
    icon: pystray.Icon,
    states: list[DisplayState],
    companion: TaskbarDisplay,
) -> None:
    """Show every tracked provider on the one tray icon and the one label.

    Both surfaces take the whole list: the icon condenses it into a colour and
    a hover summary, the label draws a row per provider.
    """
    tray.apply(icon, processor.tray_status(states))
    label = processor.taskbar_label(states)
    companion.update(label.segments, label.tooltip)


def _toggle_taskbar_visibility(
    companion: TaskbarDisplay,
    persist: Callable[[bool], None],
) -> None:
    """Flip the taskbar label, for the tray menu entry that still offers it."""
    _set_taskbar_visibility(companion, persist, not companion.visible)


def _set_taskbar_visibility(
    companion: TaskbarDisplay,
    persist: Callable[[bool], None],
    visible: bool,
) -> None:
    """Show or hide the taskbar label and remember the choice for next launch.

    This runs on a UI thread, where an escaping exception would surface only as
    a stderr traceback nobody sees in a windowed build, so a failed config write
    is logged and the change still takes effect.
    """
    companion.set_visible(visible)
    try:
        persist(visible)
    except Exception:
        log.exception("unable to persist taskbar visibility")


def _toggle_session_refresh(
    nudgers: list[cli_refresher.SessionNudger],
    persist: Callable[[bool], None],
) -> None:
    """Flip the CLI nudge, for whichever surface offers it as a toggle."""
    _set_session_refresh(nudgers, persist, not _session_refresh_is_enabled(nudgers))


def _set_session_refresh(
    nudgers: list[cli_refresher.SessionNudger],
    persist: Callable[[bool], None],
    enabled: bool,
) -> None:
    """Switch every provider's CLI nudge on or off together.

    One switch for both: the nudge exists to renew an expired token, and a
    user who wants that automated wants it for whichever CLI needs it.
    """
    for nudger in nudgers:
        nudger.set_enabled(enabled)
    try:
        persist(enabled)
    except Exception:
        log.exception("unable to persist session refresh setting")


def _set_session_refresh_cooldown(
    nudgers: list[cli_refresher.SessionNudger],
    config: Config,
    persist: Callable[[float], None],
    seconds: int,
) -> None:
    """Change how long a nudge waits before it may run again.

    The running nudgers are told as well as the file, or the new gap would
    only take effect at the next launch.
    """
    config.session_refresh.cooldown_seconds = seconds
    for nudger in nudgers:
        nudger.set_cooldown_seconds(seconds)
    _persist(persist, seconds, "session refresh cooldown")


def _session_refresh_is_enabled(
    nudgers: list[cli_refresher.SessionNudger],
) -> bool:
    """Return the shared nudge setting, as the tray checkmark reads it."""
    return any(nudger.enabled for nudger in nudgers)


@dataclass
class CodexTracking:
    """Whether Codex is being tracked at all."""

    enabled: bool


def _toggle_codex_tracking(
    tracking: CodexTracking,
    persist: Callable[[bool], None],
    wake_poll_loop: Callable[[], None] = lambda: None,
) -> None:
    """Start or stop tracking Codex, and ask the loop to redraw at once.

    Only a poll decides which providers are shown, so without the wake-up the
    switch would appear to do nothing for up to a polling interval.

    Like the other toggles this runs on a UI thread, where an escaping
    exception would surface only as a stderr traceback nobody sees in a
    windowed build, so a failed config write is logged and the switch still
    takes effect.
    """
    _set_codex_tracking(tracking, persist, not tracking.enabled, wake_poll_loop)


def _set_codex_tracking(
    tracking: CodexTracking,
    persist: Callable[[bool], None],
    enabled: bool,
    wake_poll_loop: Callable[[], None] = lambda: None,
) -> None:
    """Start or stop tracking Codex, and ask the loop to redraw at once."""
    tracking.enabled = enabled
    try:
        persist(tracking.enabled)
    except Exception:
        log.exception("unable to persist codex tracking setting")
    wake_poll_loop()


def _persist(save: Callable[[object], None], value: object, described_as: str) -> None:
    """Write one setting to the config file, logging rather than raising.

    Every caller runs on a UI thread and has already changed the running app,
    so a file that cannot be written must not undo that or take the thread down
    with it.
    """
    try:
        save(value)
    except Exception:
        log.exception("unable to persist the %s setting", described_as)


def _set_poll_interval(
    config: Config,
    pollers: list["ProviderPoller"],
    persist: Callable[[int], None],
    wake_poll_loop: Callable[[], None],
    seconds: int,
) -> None:
    """Change how often usage is fetched, from the next wait onwards.

    A poller that has backed off after an error keeps an interval of its own,
    so each one is reset too: otherwise the new setting would be ignored for as
    long as the backoff lasted.
    """
    config.polling.interval_seconds = seconds
    for poller in pollers:
        poller.interval_seconds = seconds
    _persist(persist, seconds, "poll interval")
    wake_poll_loop()


def _set_threshold(
    config: Config,
    attribute: str,
    persist: Callable[[float], None],
    wake_poll_loop: Callable[[], None],
    described_as: str,
    percent: int,
) -> None:
    """Change one icon colour threshold and redraw with it at once."""
    setattr(config.thresholds, attribute, percent)
    _persist(persist, percent, described_as)
    wake_poll_loop()


# What each number in the settings window is allowed to be. The lower bounds
# are the point below which the app would be working against itself: polling
# faster than the API updates, or nudging a session every few seconds.
_MIN_POLL_INTERVAL_SECONDS = 10
_MAX_POLL_INTERVAL_SECONDS = 3600
_MIN_THRESHOLD_PERCENT = 1
_MAX_THRESHOLD_PERCENT = 99
_MIN_REFRESH_COOLDOWN_SECONDS = 60
_MAX_REFRESH_COOLDOWN_SECONDS = 86_400


def build_settings_model(
    *,
    companion: TaskbarDisplay,
    codex_tracking: CodexTracking,
    nudgers: list[cli_refresher.SessionNudger],
    pollers: list["ProviderPoller"],
    config: Config,
    log_dir: Path,
    wake_poll_loop: Callable[[], None] = lambda: None,
) -> SettingsModel:
    """Point every field in the settings window at the thing it controls.

    Each write changes the running application as well as the config file, so
    Apply means what it says: nothing here waits for the next launch.
    """
    return build_settings(
        taskbar=Switch(
            is_on=lambda: companion.visible,
            write=lambda visible: _set_taskbar_visibility(
                companion, save_taskbar_enabled, visible
            ),
            available=lambda: companion.healthy,
        ),
        codex=Switch(
            is_on=lambda: codex_tracking.enabled,
            write=lambda enabled: _set_codex_tracking(
                codex_tracking,
                save_codex_enabled,
                enabled,
                wake_poll_loop,
            ),
        ),
        session_refresh=Switch(
            is_on=lambda: _session_refresh_is_enabled(nudgers),
            write=lambda enabled: _set_session_refresh(
                nudgers,
                save_session_refresh_enabled,
                enabled,
            ),
        ),
        startup=Switch(
            is_on=lambda: _startup_registration_enabled(autostart.is_enabled),
            write=lambda enabled: _set_startup_registration(
                autostart.set_enabled, enabled
            ),
        ),
        poll_interval=Number(
            value=lambda: int(config.polling.interval_seconds),
            write=lambda seconds: _set_poll_interval(
                config, pollers, save_poll_interval_seconds, wake_poll_loop, seconds
            ),
            minimum=_MIN_POLL_INTERVAL_SECONDS,
            maximum=_MAX_POLL_INTERVAL_SECONDS,
        ),
        amber_threshold=Number(
            value=lambda: int(config.thresholds.amber_below),
            write=lambda percent: _set_threshold(
                config,
                "amber_below",
                save_amber_threshold,
                wake_poll_loop,
                "amber threshold",
                percent,
            ),
            minimum=_MIN_THRESHOLD_PERCENT,
            maximum=_MAX_THRESHOLD_PERCENT,
        ),
        red_threshold=Number(
            value=lambda: int(config.thresholds.red_below),
            write=lambda percent: _set_threshold(
                config,
                "red_below",
                save_red_threshold,
                wake_poll_loop,
                "red threshold",
                percent,
            ),
            minimum=_MIN_THRESHOLD_PERCENT,
            maximum=_MAX_THRESHOLD_PERCENT,
        ),
        refresh_cooldown=Number(
            value=lambda: int(config.session_refresh.cooldown_seconds),
            write=lambda seconds: _set_session_refresh_cooldown(
                nudgers, config, save_session_refresh_cooldown, seconds
            ),
            minimum=_MIN_REFRESH_COOLDOWN_SECONDS,
            maximum=_MAX_REFRESH_COOLDOWN_SECONDS,
        ),
        log_dir=log_dir,
    )


def create_settings_controller(
    model: SettingsModel,
    build_window: Callable[[SettingsModel], SettingsView] = create_settings_window,
    **overrides,
) -> SettingsWindowController:
    """Build the controller the tray menu opens the settings window through."""
    return SettingsWindowController(
        build_window=lambda: build_window(model),
        **overrides,
    )


def _startup_registration_enabled(check: Callable[[], bool]) -> bool:
    """Read startup state without allowing a registry error into pystray."""
    try:
        return check()
    except OSError:
        log.exception("unable to read Windows startup registration")
        return False


def _toggle_startup_registration(
    check: Callable[[], bool],
    persist: Callable[[bool], None],
) -> None:
    """Flip per-user startup registration, for the tray menu entry."""
    try:
        _set_startup_registration(persist, not check())
    except OSError:
        log.exception("unable to read Windows startup registration")


def _set_startup_registration(
    persist: Callable[[bool], None],
    enabled: bool,
) -> None:
    """Register or unregister the app for login, without crashing the caller."""
    try:
        persist(enabled)
    except OSError:
        log.exception("unable to update Windows startup registration")


def _repair_startup_registration(repair: Callable[[], bool]) -> None:
    """Self-heal an opted-in startup command after the application moves."""
    try:
        repair()
    except OSError:
        log.exception("unable to repair Windows startup registration")


def _wait_with_display_refresh(
    manual_refresh: threading.Event,
    *,
    interval_seconds: int,
    refresh_display: Callable[[], None],
    clock: Callable[[], float] = time.monotonic,
    shutdown_requested: threading.Event | None = None,
) -> bool:
    """Wait for the next fetch while refreshing relative display text each second."""
    deadline = clock() + interval_seconds
    while True:
        if shutdown_requested is not None and shutdown_requested.is_set():
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        if manual_refresh.wait(timeout=min(_DISPLAY_REFRESH_INTERVAL_SECONDS, remaining)):
            return True
        refresh_display()


def _handle_console_control_event(
    control_type: int,
    shutdown_requested: threading.Event,
    manual_refresh: threading.Event,
    icon: pystray.Icon,
) -> bool:
    """Convert Ctrl+C/Ctrl+Break into the same orderly shutdown as tray Quit."""
    if control_type not in (_CTRL_C_EVENT, _CTRL_BREAK_EVENT):
        return False
    shutdown_requested.set()
    manual_refresh.set()
    icon.stop()
    return True


def _install_console_shutdown_handler(
    shutdown_requested: threading.Event,
    manual_refresh: threading.Event,
    icon: pystray.Icon,
) -> ctypes._CFuncPtr:
    """Install a Windows console handler that consumes Ctrl+C for clean shutdown."""
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(control_type: int) -> bool:
        return _handle_console_control_event(
            control_type,
            shutdown_requested,
            manual_refresh,
            icon,
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = (ctypes.c_void_p, wintypes.BOOL)
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    if not kernel32.SetConsoleCtrlHandler(ctypes.cast(handler, ctypes.c_void_p), True):
        logging.getLogger(__name__).warning("unable to register console shutdown handler")
    return handler


def _remove_console_shutdown_handler(handler: ctypes._CFuncPtr) -> None:
    """Unregister the console handler once the pystray message loop has ended."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = (ctypes.c_void_p, wintypes.BOOL)
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.SetConsoleCtrlHandler(ctypes.cast(handler, ctypes.c_void_p), False)


def create_session_nudger(
    cfg: Config,
    manual_refresh: threading.Event,
    **overrides,
) -> cli_refresher.SessionNudger:
    """Build the CLI session nudger, re-polling as soon as a refresh succeeds.

    Waking the loop matters because the whole point of the nudge is that the
    numbers it produces are newer than the ones that triggered it.
    """
    return cli_refresher.SessionNudger(
        enabled=cfg.session_refresh.enabled,
        cooldown_seconds=cfg.session_refresh.cooldown_seconds,
        on_refreshed=manual_refresh.set,
        **overrides,
    )


def create_codex_nudger(
    cfg: Config,
    manual_refresh: threading.Event,
    **overrides,
) -> cli_refresher.SessionNudger:
    """Build Codex's nudger: the same rule as Claude, a different CLI to run."""
    return create_session_nudger(
        cfg,
        manual_refresh,
        invoke=cli_refresher.run_codex_cli,
        **overrides,
    )


def _next_poll_interval_seconds(
    current_interval_seconds: int,
    data: fetcher.ProviderUsageData,
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


def _is_successful_fetch(data: fetcher.ProviderUsageData) -> bool:
    """Return whether a fetch completed successfully enough to update freshness."""
    return data.fetch_error is None and data.status_code == 200


def _active_pollers(
    claude: object,
    codex: object,
    *,
    codex_enabled: bool,
) -> list:
    """Return the pollers to run this tick, in the order they are displayed."""
    return [claude, codex] if codex_enabled else [claude]


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
        fetch: Callable[[], ProviderUsageData],
        config: Config,
        nudger: cli_refresher.SessionNudger,
    ) -> None:
        self.provider = provider
        self._fetch = fetch
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
        data = self._fetch()
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
        return processor.process(
            self._latest,
            now=now,
            config=self._config,
            last_good=self._last_good,
            session_refresh_exhausted=self._nudger.exhausted,
            provider=self.provider,
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
        return processor.internal_error_state(now=now, provider=poller.provider)


def _acquire_single_instance(name: str = "ClaudeMonitor.SingleInstance") -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, name)
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "claudemonitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)],
    )


def main() -> None:
    # Must precede every window this process creates, including pystray's, or
    # Windows fixes the awareness at "unaware" and virtualizes the coordinates
    # the taskbar label exchanges with Explorer.
    enable_per_monitor_dpi_awareness()

    log_dir = Path(os.environ["APPDATA"]) / "claudemonitor"
    _setup_logging(log_dir)

    if not _acquire_single_instance():
        sys.exit(0)

    log.info("ClaudeMonitor starting")

    _repair_startup_registration(autostart.repair_if_enabled)

    cfg = load_config()
    manual_refresh = threading.Event()
    shutdown_requested = threading.Event()
    companion = create_taskbar_companion(initial_visible=cfg.taskbar.enabled)
    codex_tracking = CodexTracking(enabled=cfg.codex.enabled)
    # Built before the tray so its menu toggles have something to flip; the
    # poll loop starts later and closes over the same instances.
    session_nudger = create_session_nudger(cfg, manual_refresh)
    codex_nudger = create_codex_nudger(cfg, manual_refresh)
    nudgers = [session_nudger, codex_nudger]
    claude_poller = ProviderPoller(CLAUDE, fetcher.fetch, cfg, session_nudger)
    codex_poller = ProviderPoller(CODEX, codex_fetcher.fetch, cfg, codex_nudger)

    settings_window = create_settings_controller(
        build_settings_model(
            companion=companion,
            codex_tracking=codex_tracking,
            nudgers=nudgers,
            pollers=[claude_poller, codex_poller],
            config=cfg,
            log_dir=log_dir,
            wake_poll_loop=manual_refresh.set,
        )
    )

    tray.init(
        manual_refresh,
        shutdown_requested,
        taskbar_visible=lambda: companion.visible,
        toggle_taskbar=lambda: _toggle_taskbar_visibility(companion, save_taskbar_enabled),
        taskbar_healthy=lambda: companion.healthy,
        open_settings=settings_window.open,
    )

    # Seed a placeholder for every tracked provider before the label appears,
    # so it does not visibly grow from one segment to two on the first fetch.
    initial_label = processor.loading_label(
        [
            poller.provider
            for poller in _active_pollers(
                claude_poller,
                codex_poller,
                codex_enabled=codex_tracking.enabled,
            )
        ]
    )
    companion.update(initial_label.segments, initial_label.tooltip)
    companion.start()

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        while not shutdown_requested.is_set():
            notifications: list[UsageNotification] = []
            try:
                active = _active_pollers(
                    claude_poller,
                    codex_poller,
                    codex_enabled=codex_tracking.enabled,
                )
                for poller in active:
                    notifications += _poll_provider(poller)
                poll_interval_seconds = _shared_poll_interval(
                    [poller.interval_seconds for poller in active]
                )

                def build_states() -> list[DisplayState]:
                    now = datetime.now(timezone.utc)
                    return [_provider_display(poller, now) for poller in active]
            except Exception:
                log.exception("unhandled error in poll loop")
                poll_interval_seconds = cfg.polling.interval_seconds

                def build_states() -> list[DisplayState]:
                    return [
                        processor.internal_error_state(now=datetime.now(timezone.utc))
                    ]

            _apply_display(icon, build_states(), companion)
            for notification in notifications:
                tray.notify(icon, title=notification.title, message=notification.message)
            manual_refresh.clear()
            if shutdown_requested.is_set():
                break
            _wait_with_display_refresh(
                manual_refresh,
                interval_seconds=poll_interval_seconds,
                refresh_display=lambda: _apply_display(icon, build_states(), companion),
                shutdown_requested=shutdown_requested,
            )

    icon = pystray.Icon(
        "ClaudeMonitor",
        icon=tray.loading_icon(),
        title="Claude Monitor — loading…",
        menu=pystray.Menu(),
    )

    console_handler = _install_console_shutdown_handler(
        shutdown_requested,
        manual_refresh,
        icon,
    )
    try:
        icon.run(setup=setup)
    finally:
        shutdown_requested.set()
        manual_refresh.set()
        _remove_console_shutdown_handler(console_handler)
        settings_window.close()
        companion.stop()


def poll() -> None:
    """Print one fetch per provider, for `uv run poll`."""
    import json

    for provider, fetch in ((CLAUDE, fetcher.fetch), (CODEX, codex_fetcher.fetch)):
        print(f"--- {provider.label} ---")
        print(json.dumps(fetch().model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
