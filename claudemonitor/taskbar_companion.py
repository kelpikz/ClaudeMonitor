"""Coordinate the optional usage label shown beside the Windows notification area.

This module contains the ordinary Python control flow: it owns the background
thread, tracks the requested text and visibility, and calculates where the
label belongs. The difficult Windows API calls live in ``win32_taskbar_window``
behind the small ``NativeWindow`` interface below.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .models import Rect
from .processor import LOADING_TASKBAR_TEXT


log = logging.getLogger(__name__)

# Width of the label in pixels. ClaudeMonitor does not declare DPI awareness, so
# Windows virtualizes every coordinate this module sees to 96 DPI and scales the
# result for the monitor: one constant is correct at any display scaling.
_COMPANION_WIDTH = 180

# Below this width the usage text would be clipped into meaninglessness, so
# placement stops yielding space to neighbouring taskbar plugins.
_MIN_COMPANION_WIDTH = 60

# While visible, re-check taskbar geometry once per second so the label follows
# changes such as the clock growing wider or another taskbar plugin appearing.
_REPOSITION_INTERVAL_SECONDS = 1.0

# Windows 11 periodically raises the taskbar above every topmost window, so the
# unattached fallback must re-stack itself — but far less often than it polls.
_TOPMOST_REASSERT_INTERVAL_SECONDS = 5.0

# Windows only broadcasts theme changes to top-level windows, so an embedded
# label has to re-read the setting itself — slowly, since it rarely changes.
_THEME_REFRESH_INTERVAL_SECONDS = 5.0

# Consecutive native failures tolerated before the window is rebuilt. Explorer
# restarting invalidates our HWND, and only a fresh window recovers from that.
_MAX_CONSECUTIVE_FAILURES = 3

# Failed rebuilds tolerated before the feature reports itself unavailable so the
# tray menu can stop claiming a label is being shown.
_MAX_REBUILDS_BEFORE_UNHEALTHY = 2

# Pause after a native failure so a persistent fault cannot spin the CPU. The
# wait doubles each time, because a fault that survives one retry rarely clears
# on the next and the retries themselves fill the log.
_FAILURE_BACKOFF_SECONDS = 1.0
_FAILURE_BACKOFF_CAP_SECONDS = 60.0


def retry_delay_seconds(consecutive_failures: int, base: float, cap: float) -> float:
    """Return how long to wait before the next attempt, doubling up to a ceiling."""
    return min(cap, base * 2 ** max(0, consecutive_failures - 1))


def _overlaps_vertically(first: Rect, second: Rect) -> bool:
    """Return whether two rectangles share any horizontal band of the screen."""
    return first.top < second.bottom and second.top < first.bottom


def leftmost_abutting_edge(
    anchor: Rect,
    children: list[Rect],
    tolerance: int = 8,
) -> int:
    """Find the free edge before plugins placed immediately left of the clock.

    Starting at the anchor's left edge, walk left across rectangles that touch
    each other. Only windows sharing the anchor's row are considered, so a
    flyout floating above the taskbar cannot displace the label. The small
    tolerance accounts for Windows adding a few pixels between adjacent items.
    """
    neighbours = [child for child in children if _overlaps_vertically(anchor, child)]
    edge = anchor.left
    moved = True
    while moved:
        moved = False
        for child in neighbours:
            if child.left < edge and abs(child.right - edge) <= tolerance:
                edge = child.left
                moved = True
    return edge


def taskbar_child_rect(taskbar: Rect, notification: Rect, width: int) -> Rect:
    """Reserve a taskbar-relative rectangle immediately before the clock area."""
    # Both edges are clamped: a restarting Explorer can momentarily report a
    # notification area that sits outside the taskbar it belongs to.
    right = max(0, notification.left - taskbar.left)
    return Rect(
        left=max(0, right - width),
        top=0,
        right=right,
        bottom=taskbar.height,
    )


def companion_slot(
    taskbar: Rect,
    notification: Rect,
    siblings: list[Rect],
    width: int,
    minimum_width: int = _MIN_COMPANION_WIDTH,
) -> Rect:
    """Choose the label's slot, yielding to plugins only while it stays legible."""
    anchored_notification = Rect(
        left=leftmost_abutting_edge(notification, siblings),
        top=notification.top,
        right=notification.right,
        bottom=notification.bottom,
    )
    slot = taskbar_child_rect(taskbar, anchored_notification, width)
    if slot.width >= minimum_width:
        return slot

    # Stepping over every plugin left too little room, so sit against the clock
    # and accept overlapping a neighbour rather than showing an unreadable stub.
    return taskbar_child_rect(taskbar, notification, width)


class NativeWindow(Protocol):
    """High-level operations the controller needs from the Windows adapter.

    Keeping raw Windows flags and ``ctypes`` structures out of this interface
    lets the controller and its tests read like normal Python code.
    """

    def find_taskbar(self) -> int: ...
    def find_notification_area(self, taskbar: int) -> int: ...
    def get_rect(self, handle: int) -> Rect: ...
    def create_window(self, *, text: str) -> int: ...
    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool: ...
    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]: ...
    def set_colorkey_transparency(self, handle: int) -> None: ...
    def refresh_theme(self, handle: int) -> None: ...
    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None: ...
    def set_text(self, handle: int, text: str) -> None: ...
    def set_visible(self, handle: int, visible: bool) -> None: ...
    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None: ...
    def close_window(self, handle: int) -> None: ...


@dataclass
class _NativeSession:
    """Track what the live native window currently shows and where it sits."""

    handle: int
    attached: bool
    rendered_text: str
    rendered_visible: bool = False
    position: Rect | None = None
    topmost_asserted_at: float | None = None
    theme_read_at: float | None = None
    reported_empty_slot: bool = False


class TaskbarCompanion:
    """Keep one native usage label synchronized with application display state.

    Windows requires a window to be created, painted, and destroyed on the same
    thread. This controller therefore owns a small background UI thread. When
    the user hides taskbar usage, that thread waits on a condition instead of
    continuing to query and reposition the hidden window.
    """

    def __init__(
        self,
        *,
        native: NativeWindow | None = None,
        initial_visible: bool = True,
        clock: Callable[[], float] = time.monotonic,
        failure_backoff_seconds: float = _FAILURE_BACKOFF_SECONDS,
    ) -> None:
        if native is None:
            # Import lazily so this portable controller can be imported (and
            # unit-tested) without loading Windows DLLs.
            from .win32_taskbar_window import Win32TaskbarWindow

            native = Win32TaskbarWindow()
        self._native = native
        self._clock = clock
        self._failure_backoff_seconds = failure_backoff_seconds
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._display_changed = threading.Condition()
        self._text = LOADING_TASKBAR_TEXT
        self._visible = initial_visible
        self._healthy = True

    @property
    def visible(self) -> bool:
        """Return the user-selected visibility state."""
        with self._display_changed:
            return self._visible

    @property
    def healthy(self) -> bool:
        """Return whether a native label is actually being shown.

        False once repeated rebuilds have failed, so the tray menu can say the
        feature is unavailable instead of showing a checkmark for nothing.
        """
        return self._healthy

    def update(self, text: str) -> None:
        """Store text for the UI thread to paint on its next visible pass."""
        with self._display_changed:
            self._text = text
            self._display_changed.notify_all()

    def set_visible(self, visible: bool) -> None:
        """Show or hide the companion and wake the UI thread when showing it."""
        with self._display_changed:
            self._visible = visible
            self._display_changed.notify_all()

    def start(self) -> None:
        """Start the native UI and message pump on their owning thread."""
        if self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ClaudeMonitorTaskbarCompanion",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Wake the UI thread, request shutdown, and briefly wait for cleanup."""
        self._stop_requested.set()
        with self._display_changed:
            self._display_changed.notify_all()
        thread = self._thread
        if thread is threading.current_thread():
            # Called from the UI thread itself; it will unwind on its own, and
            # clearing _thread here would let a later start() run two of them.
            return
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                # Leave the stop flag set so the stuck thread still exits later.
                log.warning("taskbar companion thread did not stop in time")
                return
        self._thread = None

    def _display_state(self) -> tuple[str, bool]:
        """Take one consistent snapshot of text and visibility requests."""
        with self._display_changed:
            return self._text, self._visible

    def _wait_until_visible(self) -> None:
        """Sleep without polling until the user shows the companion or quits."""
        with self._display_changed:
            self._display_changed.wait_for(
                lambda: self._visible or self._stop_requested.is_set()
            )

    def _run(self) -> None:
        """Keep a native label alive and current until shutdown is requested.

        A single Windows call failing — an Explorer restart, a sibling window
        destroyed mid-measurement — must never end the label permanently, so
        each pass is isolated and a persistently broken window is rebuilt.
        """
        session: _NativeSession | None = None
        failures = 0
        rebuilds = 0
        try:
            while not self._stop_requested.is_set():
                try:
                    if session is None:
                        session = self._open_session()
                    self._run_one_pass(session)
                except Exception:
                    failures += 1
                    self._log_failure(failures)
                    if failures % _MAX_CONSECUTIVE_FAILURES == 0:
                        self._close_session(session)
                        session = None
                        rebuilds += 1
                        self._healthy = rebuilds < _MAX_REBUILDS_BEFORE_UNHEALTHY
                    self._stop_requested.wait(
                        retry_delay_seconds(
                            failures,
                            base=self._failure_backoff_seconds,
                            cap=_FAILURE_BACKOFF_CAP_SECONDS,
                        )
                    )
                    continue

                if failures:
                    log.info("taskbar companion recovered after %s failures", failures)
                failures = 0
                rebuilds = 0
                self._healthy = True
        finally:
            self._close_session(session)

    def _log_failure(self, failures: int) -> None:
        """Report a failed pass without burying the log under repeat tracebacks.

        The full traceback explains a new problem exactly once; a persistent one
        would otherwise overwrite the entire rotating log within the hour.
        """
        if failures == 1:
            log.exception("taskbar companion pass failed")
        elif failures % _MAX_CONSECUTIVE_FAILURES == 0:
            log.warning(
                "taskbar companion still failing after %s attempts; rebuilding window",
                failures,
            )

    def _open_session(self) -> _NativeSession:
        """Create the native label, attach it to Explorer, and leave it hidden.

        Any failure after the window exists destroys it before propagating,
        because the caller has no handle to clean up with.
        """
        rendered_text, _requested_visible = self._display_state()
        taskbar = self._native.find_taskbar()
        handle = self._native.create_window(text=rendered_text)
        try:
            # Prefer a true taskbar child. If Explorer rejects the attachment,
            # the native adapter leaves the window as a normal popup and the
            # controller keeps it above the taskbar as a graceful fallback.
            attached = self._native.attach_to_taskbar(handle, taskbar)
            if not attached:
                log.warning("taskbar parenting failed; using topmost screen popup")

            # Both a child and a popup need the black background keyed out;
            # skipping it would paint a solid rectangle over the taskbar.
            self._native.set_colorkey_transparency(handle)
        except Exception:
            self._native.close_window(handle)
            raise

        log.info(
            "taskbar companion window=%s initialized attached=%s",
            handle,
            attached,
        )
        return _NativeSession(
            handle=handle,
            attached=attached,
            rendered_text=rendered_text,
        )

    def _close_session(self, session: _NativeSession | None) -> None:
        """Destroy the native label, if one exists."""
        if session is not None:
            self._native.close_window(session.handle)

    def _run_one_pass(self, session: _NativeSession) -> None:
        """Synchronize one round of text, placement, visibility, and messages."""
        requested_text, requested_visible = self._display_state()

        if requested_text != session.rendered_text:
            self._native.set_text(session.handle, requested_text)
            session.rendered_text = requested_text

        if not requested_visible:
            self._hide(session)
            # Hidden taskbar usage performs no message pumping, geometry
            # queries, theme reads, or topmost re-assertion.
            self._wait_until_visible()
            return

        # One clock reading per pass keeps the periodic work in step and makes
        # the intervals straightforward to assert in tests.
        now = self._clock()
        self._refresh_theme_if_due(session, now)

        # Place the window before revealing it, otherwise switching the label on
        # flashes a speck at the taskbar's left edge.
        self._reposition(session, now)
        if session.position is not None:
            self._show(session)

        # Dispatch paint and other Windows messages only while the label is
        # visible. This returns at least once per second so display state and
        # taskbar geometry can be refreshed.
        self._native.pump_messages(self._stop_requested, _REPOSITION_INTERVAL_SECONDS)

    def _refresh_theme_if_due(self, session: _NativeSession, now: float) -> None:
        """Poll Windows' light/dark setting on a slow interval.

        A window parented into the taskbar is a child window, and Windows does
        not broadcast WM_SETTINGCHANGE to child windows — so polling is the only
        way an embedded label learns the user switched to light mode.
        """
        due = (
            session.theme_read_at is None
            or now - session.theme_read_at >= _THEME_REFRESH_INTERVAL_SECONDS
        )
        if not due:
            return
        self._native.refresh_theme(session.handle)
        session.theme_read_at = now

    def _show(self, session: _NativeSession) -> None:
        """Reveal the label if it is not already showing."""
        if not session.rendered_visible:
            self._native.set_visible(session.handle, True)
            session.rendered_visible = True

    def _hide(self, session: _NativeSession) -> None:
        """Hide the label if it is not already hidden."""
        if session.rendered_visible:
            self._native.set_visible(session.handle, False)
            session.rendered_visible = False

    def _reposition(self, session: _NativeSession, now: float) -> None:
        """Move the label when the taskbar changed or its z-order needs renewing."""
        new_position = self._compute_position(session)
        if new_position.width <= 0:
            # Explorer can briefly report a taskbar and a notification area whose
            # bounds contradict each other. SetWindowPos rejects the resulting
            # rectangle, so skip the move rather than start a failure cycle.
            if not session.reported_empty_slot:
                log.warning("taskbar reported an empty slot %s; skipping move", new_position)
                session.reported_empty_slot = True
            return
        session.reported_empty_slot = False

        moved = new_position != session.position
        reassert_topmost = not session.attached and self._topmost_is_stale(session, now)
        if not moved and not reassert_topmost:
            return

        self._native.move_window(
            session.handle,
            new_position,
            topmost=not session.attached,
        )

        # Record the new placement only once Windows has accepted it, so a
        # failed move is retried instead of being remembered as applied.
        if moved and session.position is not None:
            log.info("taskbar companion repositioned to %s", new_position)
        session.position = new_position
        if not session.attached:
            session.topmost_asserted_at = now

    def _topmost_is_stale(self, session: _NativeSession, now: float) -> bool:
        """Return whether the fallback popup is due to re-claim topmost z-order."""
        if session.topmost_asserted_at is None:
            return True
        return now - session.topmost_asserted_at >= _TOPMOST_REASSERT_INTERVAL_SECONDS

    def _compute_position(self, session: _NativeSession) -> Rect:
        """Calculate the label's coordinates from the taskbar and clock areas."""
        taskbar = self._native.find_taskbar()
        notification = self._native.find_notification_area(taskbar)
        taskbar_rect = self._native.get_rect(taskbar)
        notification_rect = self._native.get_rect(notification)
        siblings = self._native.list_sibling_rects(taskbar, session.handle)

        position = companion_slot(
            taskbar_rect,
            notification_rect,
            siblings,
            width=_COMPANION_WIDTH,
        )
        if session.attached:
            # Child windows use coordinates relative to their parent taskbar.
            return position

        # A fallback popup is a normal screen window, so convert the same slot
        # into absolute screen coordinates.
        return Rect(
            left=position.left + taskbar_rect.left,
            top=position.top + taskbar_rect.top,
            right=position.right + taskbar_rect.left,
            bottom=position.bottom + taskbar_rect.top,
        )


class DisabledTaskbarCompanion:
    """Stand in for the companion when no native label can be created.

    The taskbar label is an optional extra, so a machine where the Windows
    adapter cannot even be constructed must still get its tray icon. This
    absorbs every call the application makes and reports itself unavailable.
    """

    visible = False
    healthy = False

    def update(self, text: str) -> None:
        """Discard display text; there is no window to paint it on."""

    def set_visible(self, visible: bool) -> None:
        """Ignore visibility changes; the feature is unavailable."""

    def start(self) -> None:
        """Start nothing."""

    def stop(self) -> None:
        """Stop nothing."""


# What the rest of the application depends on: either a live companion or the
# stand-in that quietly absorbs the same calls.
TaskbarDisplay = TaskbarCompanion | DisabledTaskbarCompanion


def create_taskbar_companion(
    *,
    initial_visible: bool,
    build_native: Callable[[], NativeWindow] | None = None,
) -> TaskbarDisplay:
    """Build the taskbar companion, degrading instead of aborting startup.

    Loading the Windows adapter touches ``user32`` exports that do not exist on
    every supported build. Failing here must cost the user their taskbar label,
    not their tray icon.
    """
    try:
        native = build_native() if build_native is not None else None
        return TaskbarCompanion(native=native, initial_visible=initial_visible)
    except Exception:
        log.exception("taskbar companion unavailable; continuing with the tray only")
        return DisabledTaskbarCompanion()
