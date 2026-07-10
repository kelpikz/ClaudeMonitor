from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import pystray

from . import fetcher, processor, tray
from .config import load_config
from .notifications import ThresholdNotifier

_ERROR_ALREADY_EXISTS = 183

_POLL_INTERVAL_RECOVERY_STEP_SECONDS = 5
_POLL_INTERVAL_BACKOFF_FACTOR = 2
_POLL_INTERVAL_CAP_SECONDS = 600
_DISPLAY_REFRESH_INTERVAL_SECONDS = 1
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1


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


def _is_successful_fetch(data: fetcher.AnthropicUsageData) -> bool:
    """Return whether a fetch completed successfully enough to update freshness."""
    return data.fetch_error is None and data.status_code == 200


def _next_poll_interval_seconds(
    current_interval_seconds: int,
    data: fetcher.AnthropicUsageData,
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
    log_dir = Path(os.environ["APPDATA"]) / "claudemonitor"
    _setup_logging(log_dir)

    if not _acquire_single_instance():
        sys.exit(0)

    log = logging.getLogger(__name__)
    log.info("ClaudeMonitor starting")

    cfg = load_config()
    manual_refresh = threading.Event()
    shutdown_requested = threading.Event()
    tray.init(manual_refresh, log_dir, shutdown_requested)

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        # Remember the most recent successful fetch so a later rate-limit (429)
        # can keep showing real usage instead of a grey "offline" icon.
        last_good: fetcher.AnthropicUsageData | None = None
        current_poll_interval_seconds = cfg.polling.interval_seconds
        threshold_notifier = ThresholdNotifier()
        while not shutdown_requested.is_set():
            notifications = []
            try:
                data = fetcher.fetch()
                notifications = threshold_notifier.check(data)
                if data.fetch_error is None and data.five_hour is not None:
                    last_good = data
                current_poll_interval_seconds = _next_poll_interval_seconds(
                    current_poll_interval_seconds,
                    data,
                    baseline_seconds=cfg.polling.interval_seconds,
                )
                def build_state() -> processor.DisplayState:
                    return processor.process(
                        data,
                        now=datetime.now(timezone.utc),
                        config=cfg,
                        last_good=last_good,
                    )
            except Exception:
                log.exception("unhandled error in poll loop")
                def build_state() -> processor.DisplayState:
                    return processor.internal_error_state(now=datetime.now(timezone.utc))

            tray.apply(icon, build_state())
            for notification in notifications:
                tray.notify(icon, title=notification.title, message=notification.message)
            manual_refresh.clear()
            if shutdown_requested.is_set():
                break
            _wait_with_display_refresh(
                manual_refresh,
                interval_seconds=current_poll_interval_seconds,
                refresh_display=lambda: tray.apply(icon, build_state()),
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


def poll() -> None:
    import json
    data = fetcher.fetch()
    print(json.dumps(data.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
