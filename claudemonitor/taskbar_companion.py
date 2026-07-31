"""Coordinate the optional usage label shown beside the Windows notification area.

This module contains the ordinary Python control flow: it owns the background
thread, tracks the requested text and visibility, and calculates where the
label belongs. The difficult Windows API calls live in ``win32_taskbar_window``
behind the small ``NativeWindow`` interface below.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol


log = logging.getLogger(__name__)

# Text shown during startup before the first API response has been processed.
_INITIAL_DISPLAY_TEXT = "Claude: loading..."
_COMPANION_WIDTH = 180

# While visible, re-check taskbar geometry once per second so the label follows
# changes such as the clock growing wider or another taskbar plugin appearing.
_REPOSITION_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class Rect:
    """Represent the four screen coordinates around a rectangular area."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Return the rectangle width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Return the rectangle height in pixels."""
        return self.bottom - self.top


def leftmost_abutting_edge(
    anchor_left: int,
    children: list[Rect],
    tolerance: int = 8,
) -> int:
    """Find the free edge before plugins placed immediately left of the clock.

    Starting at the notification area's left edge, walk left across rectangles
    that touch each other. The small tolerance accounts for Windows adding a
    few pixels of spacing between adjacent taskbar items.
    """
    anchor = anchor_left
    moved = True
    while moved:
        moved = False
        for child in children:
            if child.left < anchor and abs(child.right - anchor) <= tolerance:
                anchor = child.left
                moved = True
    return anchor


def taskbar_child_rect(taskbar: Rect, notification: Rect, width: int) -> Rect:
    """Reserve a taskbar-relative rectangle immediately before the clock area."""
    right = notification.left - taskbar.left
    return Rect(
        left=max(0, right - width),
        top=0,
        right=right,
        bottom=taskbar.height,
    )


class NativeWindow(Protocol):
    """High-level operations the controller needs from the Windows adapter.

    Keeping raw Windows flags and ``ctypes`` structures out of this interface
    lets the controller and its tests read like normal Python code.
    """

    def find_taskbar(self) -> int: ...
    def find_notification_area(self, taskbar: int) -> int: ...
    def get_rect(self, handle: int) -> Rect: ...
    def create_window(self, *, text: str, visible: bool) -> int: ...
    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool: ...
    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]: ...
    def set_colorkey_transparency(self, handle: int) -> None: ...
    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None: ...
    def set_text(self, handle: int, text: str) -> None: ...
    def set_visible(self, handle: int, visible: bool) -> None: ...
    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None: ...
    def close_window(self, handle: int) -> None: ...


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
    ) -> None:
        if native is None:
            # Import lazily to keep this portable controller independent from
            # the module that loads Windows DLLs.
            from .win32_taskbar_window import Win32TaskbarWindow

            native = Win32TaskbarWindow()
        self._native = native
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._display_changed = threading.Condition()
        self._text = _INITIAL_DISPLAY_TEXT
        self._visible = initial_visible

    @property
    def visible(self) -> bool:
        """Return the user-selected visibility state."""
        with self._display_changed:
            return self._visible

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
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

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
        """Create, synchronize, position, and finally destroy the native label."""
        window_handle = 0
        try:
            rendered_text, rendered_visible = self._display_state()
            taskbar = self._native.find_taskbar()
            window_handle = self._native.create_window(
                text=rendered_text,
                visible=rendered_visible,
            )

            # Prefer a true taskbar child. If Explorer rejects the attachment,
            # the native adapter leaves the window as a normal popup and the
            # controller keeps it above the taskbar as a graceful fallback.
            attached = self._native.attach_to_taskbar(window_handle, taskbar)
            if attached:
                self._native.set_colorkey_transparency(window_handle)
            else:
                log.warning("taskbar parenting failed; using topmost screen popup")

            position: Rect | None = None
            log.info(
                "taskbar companion window=%s initialized attached=%s visible=%s",
                window_handle,
                attached,
                rendered_visible,
            )

            while not self._stop_requested.is_set():
                requested_text, requested_visible = self._display_state()

                if requested_text != rendered_text:
                    self._native.set_text(window_handle, requested_text)
                    rendered_text = requested_text
                if requested_visible != rendered_visible:
                    self._native.set_visible(window_handle, requested_visible)
                    rendered_visible = requested_visible

                if not rendered_visible:
                    # Hidden taskbar usage performs no message pumping,
                    # geometry queries, or topmost re-assertion.
                    self._wait_until_visible()
                    continue

                new_position = self._compute_position(window_handle, attached=attached)
                should_move = new_position != position or not attached
                if should_move:
                    if position is not None and new_position != position:
                        log.info("taskbar companion repositioned to %s", new_position)
                    position = new_position
                    self._native.move_window(
                        window_handle,
                        position,
                        topmost=not attached,
                    )

                # Dispatch paint and other Windows messages only while the
                # label is visible. This returns at least once per second so
                # display state and taskbar geometry can be refreshed.
                self._native.pump_messages(
                    self._stop_requested,
                    _REPOSITION_INTERVAL_SECONDS,
                )
        except Exception:
            log.exception("native taskbar companion failed")
        finally:
            if window_handle:
                self._native.close_window(window_handle)

    def _compute_position(self, window_handle: int, *, attached: bool) -> Rect:
        """Calculate the label's coordinates from the taskbar and clock areas."""
        taskbar = self._native.find_taskbar()
        notification = self._native.find_notification_area(taskbar)
        taskbar_rect = self._native.get_rect(taskbar)
        notification_rect = self._native.get_rect(notification)
        siblings = self._native.list_sibling_rects(taskbar, window_handle)

        # Leave room for compatible plugins already sitting before the clock.
        anchored_notification = Rect(
            left=leftmost_abutting_edge(notification_rect.left, siblings),
            top=notification_rect.top,
            right=notification_rect.right,
            bottom=notification_rect.bottom,
        )
        position = taskbar_child_rect(taskbar_rect, anchored_notification, _COMPANION_WIDTH)
        if attached:
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
