from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import pystray

from . import autostart, cli_refresher, fetcher, settings, tray
from .config import Config, load_config
from .models import DisplayState
from .poll_cycle import PollCycle
from .taskbar_companion import TaskbarDisplay, create_taskbar_companion
from .win32_taskbar_window import enable_per_monitor_dpi_awareness

_ERROR_ALREADY_EXISTS = 183
log = logging.getLogger(__name__)

_DISPLAY_REFRESH_INTERVAL_SECONDS = 1
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1


class TrayAndTaskbarDisplay:
    """Push one processed state to both surfaces the application owns.

    This is the adapter behind ``PollCycle``'s ``Display`` seam; the recording
    double in the tests is the other one.
    """

    def __init__(
        self,
        presenter: tray.TrayPresenter,
        icon: pystray.Icon,
        companion: TaskbarDisplay,
    ) -> None:
        self._presenter = presenter
        self._icon = icon
        self._companion = companion

    def apply(self, state: DisplayState) -> None:
        """Update the tray icon and the taskbar label from one state."""
        self._presenter.apply(self._icon, state)
        self._companion.update(state.taskbar_text, state.tooltip)

    def notify(self, title: str, message: str) -> None:
        """Show a desktop notification through the tray icon."""
        self._presenter.notify(self._icon, title, message)


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


def create_tray_presenter(
    *,
    manual_refresh: threading.Event,
    shutdown_requested: threading.Event,
    log_dir: Path,
    companion: TaskbarDisplay,
    session_nudger: cli_refresher.SessionNudger,
) -> tray.TrayPresenter:
    """Wire every tray menu entry to the setting that owns its value."""
    return tray.TrayPresenter(
        tray.TrayActions(
            manual_refresh=manual_refresh,
            log_dir=log_dir,
            shutdown_requested=shutdown_requested,
            taskbar=settings.taskbar_setting(companion),
            session_refresh=settings.session_refresh_setting(session_nudger),
            startup=settings.startup_setting(),
            taskbar_healthy=lambda: companion.healthy,
        )
    )


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
    # Built before the tray so its menu toggle has something to flip; the poll
    # cycle starts later and shares the same instance.
    session_nudger = create_session_nudger(cfg, manual_refresh)

    presenter = create_tray_presenter(
        manual_refresh=manual_refresh,
        shutdown_requested=shutdown_requested,
        log_dir=log_dir,
        companion=companion,
        session_nudger=session_nudger,
    )
    companion.start()

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        cycle = PollCycle(
            cfg,
            display=TrayAndTaskbarDisplay(presenter, icon, companion),
            nudger=session_nudger,
        )
        cycle.run_until(
            shutdown_requested,
            manual_refresh,
            lambda seconds, refresh: _wait_with_display_refresh(
                manual_refresh,
                interval_seconds=seconds,
                refresh_display=refresh,
                shutdown_requested=shutdown_requested,
            ),
        )

    icon = pystray.Icon(
        "ClaudeMonitor",
        icon=presenter.loading_icon(),
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
        companion.stop()


def poll() -> None:
    import json
    data = fetcher.fetch()
    print(json.dumps(data.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
