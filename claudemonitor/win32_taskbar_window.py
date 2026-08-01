"""Low-level Windows implementation for the taskbar usage label.

Most of ClaudeMonitor is ordinary Python. This module is the deliberately
isolated exception: it translates readable operations such as "move the label"
or "change its text" into calls to Windows' ``user32`` and ``gdi32`` DLLs.

The Windows vocabulary itself — constants, C structures, and function
signatures — lives in ``win32_bindings`` so this file describes only behavior.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

from .models import Rect
from .win32_bindings import (
    CLASS_NAME,
    CS_HREDRAW,
    CS_VREDRAW,
    DEFAULT_GUI_FONT,
    DT_CENTER,
    DT_SINGLELINE,
    DT_VCENTER,
    ERROR_CLASS_ALREADY_EXISTS,
    GDI32_SIGNATURES,
    GW_CHILD,
    GW_HWNDNEXT,
    GWL_EXSTYLE,
    GWL_STYLE,
    HWND_TOPMOST,
    IDC_ARROW,
    KERNEL32_SIGNATURES,
    LWA_COLORKEY,
    NONCLIENTMETRICSW,
    PAINTSTRUCT,
    PM_REMOVE,
    SPI_GETNONCLIENTMETRICS,
    SW_HIDE,
    SW_SHOWNOACTIVATE,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    TRANSPARENT_BACKGROUND,
    TRANSPARENT_COLORKEY,
    USER32_SIGNATURES,
    WM_PAINT,
    WM_QUIT,
    WM_SETTINGCHANGE,
    WM_THEMECHANGED,
    WNDCLASSEXW,
    WNDPROC,
    WS_CHILD,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_POPUP,
    apply_signatures,
    foreground_color_for_theme,
    system_uses_light_theme,
)

log = logging.getLogger(__name__)

# How often the message pump checks for shutdown while waiting for messages.
_PUMP_POLL_SECONDS = 0.05


class Win32TaskbarWindow:
    """Translate the controller's simple requests into native Windows calls.

    A *handle* in this class is just Windows' numeric identifier for a window.
    ``user32`` manages windows and messages; ``gdi32`` draws the text; and
    ``kernel32`` identifies this running application while registering the
    custom window type.
    """

    def __init__(self) -> None:
        # WinDLL loads each system library and, with use_last_error enabled,
        # lets ctypes.get_last_error() read failures from the calling thread.
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        missing = (
            apply_signatures(self._user32, USER32_SIGNATURES)
            + apply_signatures(self._gdi32, GDI32_SIGNATURES)
            + apply_signatures(self._kernel32, KERNEL32_SIGNATURES)
        )
        if missing:
            # Every entry the label actually depends on has shipped since
            # Windows XP, so this is diagnostic rather than fatal.
            log.warning("Windows does not export %s; continuing without it", missing)

        # Keep the callback object alive as long as Windows may call it. If it
        # were garbage-collected, a later paint message could crash the process.
        self._window_proc_callback = WNDPROC(self._window_proc)

        # DrawTextW reads this latest value whenever Windows sends WM_PAINT.
        self._window_text = ""

        # GDI objects are created once per process and shared by every window
        # this adapter registers, so recreating the label never leaks handles.
        # A resolved font may legitimately be None (a NULL handle), so the flag
        # rather than the value records that the lookup already happened.
        self._font_handle: int | None = None
        self._font_resolved = False
        self._background_brush: int | None = None
        self._foreground_color = foreground_color_for_theme(
            uses_light_theme=system_uses_light_theme()
        )

    # --- Discovery -------------------------------------------------------

    def find_taskbar(self) -> int:
        """Find Explorer's primary taskbar by its stable Windows class name."""
        # FindWindowW searches top-level windows. Passing no title means only
        # Explorer's documented class name is used for the lookup.
        handle = self._user32.FindWindowW("Shell_TrayWnd", None)
        # A zero HWND means no matching taskbar exists, usually while Explorer
        # is restarting or before the desktop shell is ready.
        if not handle:
            raise RuntimeError("Windows taskbar was not found")
        return handle

    def find_notification_area(self, taskbar: int) -> int:
        """Find the clock-and-icons area inside the supplied taskbar."""
        # FindWindowExW restricts the search to direct children of the taskbar.
        # TrayNotifyWnd is Explorer's container for the clock and tray icons.
        handle = self._user32.FindWindowExW(taskbar, None, "TrayNotifyWnd", None)
        if not handle:
            raise RuntimeError("Windows notification area was not found")
        return handle

    def get_rect(self, handle: int) -> Rect:
        """Read a window's outer bounds in absolute screen coordinates."""
        # Windows fills this mutable RECT structure through the pointer passed
        # to GetWindowRect; it uses screen coordinates even for child windows.
        raw = wintypes.RECT()
        if not self._user32.GetWindowRect(handle, ctypes.byref(raw)):
            raise ctypes.WinError(ctypes.get_last_error())

        # Convert the mutable ctypes structure into the immutable Python value
        # shared with the controller and tests.
        return Rect(raw.left, raw.top, raw.right, raw.bottom)

    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]:
        """Collect visible windows sharing the taskbar so placement can avoid them."""
        # Accumulate plain Python rectangles; the controller decides which
        # adjacent rectangles actually affect placement.
        rects: list[Rect] = []

        # GetWindow(GW_CHILD) starts at the first direct taskbar child. Repeated
        # GW_HWNDNEXT calls then walk its siblings without recursion.
        child = self._user32.GetWindow(taskbar, GW_CHILD)
        while child:
            rect = self._sibling_rect(child, exclude_handle)
            if rect is not None:
                rects.append(rect)

            # Advance to the next sibling; a zero return ends enumeration.
            child = self._user32.GetWindow(child, GW_HWNDNEXT)
        return rects

    def _sibling_rect(self, child: int, exclude_handle: int) -> Rect | None:
        """Measure one taskbar child, or return None when it cannot affect layout."""
        # Ignore our own HWND and hidden Explorer helpers. IsWindowVisible
        # reports the effective WS_VISIBLE state of each sibling.
        if child == exclude_handle or not self._user32.IsWindowVisible(child):
            return None
        try:
            rect = self.get_rect(child)
        except OSError:
            # Taskbar children are transient: tooltips and flyouts can be
            # destroyed between enumeration and measurement. One that has
            # already vanished simply is not a sibling.
            return None
        if rect.width <= 0 or rect.height <= 0:
            return None
        return rect

    # --- Lifecycle -------------------------------------------------------

    def create_window(self, *, text: str) -> int:
        """Create a hidden, non-activating popup that Explorer can later adopt.

        The window is always created hidden so the controller can position it
        before revealing it; otherwise a 1x1 speck flashes at the screen origin.
        """
        # CreateWindowExW can only use a class after RegisterClassExW has taught
        # Windows its name, paint callback, and background brush.
        self._register_class()

        # Preserve the same initial value locally because WM_PAINT reads from
        # this Python field rather than querying Windows for the title.
        self._window_text = text

        # Extended styles keep this helper out of Alt+Tab and prevent focus
        # stealing; WS_VISIBLE is deliberately omitted.
        extended_style = WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE

        # GetModuleHandleW(None) identifies this running executable, which must
        # match the module used when the custom window class was registered.
        module_handle = self._kernel32.GetModuleHandleW(None)

        # CreateWindowExW returns the HWND used by every later operation. The
        # initial 1x1 size costs nothing while the window is still hidden;
        # no parent/menu/creation payload is needed at this stage.
        handle = self._user32.CreateWindowExW(
            extended_style,
            CLASS_NAME,
            text,
            WS_POPUP,
            0,
            0,
            1,
            1,
            None,
            None,
            module_handle,
            None,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def close_window(self, handle: int) -> None:
        """Destroy the label on the same thread that created it."""
        # IsWindow protects cleanup from a stale handle if Explorer or Windows
        # already destroyed the native label.
        if self._user32.IsWindow(handle):
            # DestroyWindow releases the HWND and sends final destruction
            # messages. Calling it on the creating thread satisfies Win32 rules.
            self._user32.DestroyWindow(handle)

    def _register_class(self) -> None:
        """Teach Windows how to create and repaint this process's label windows."""
        # WNDCLASSEXW describes a reusable window *type*, not an individual
        # window. CreateWindowExW later instantiates this description.
        window_class = WNDCLASSEXW()

        # Windows uses cbSize to determine which version of the structure it
        # received and which trailing fields are safe to read.
        window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)

        # Repaint the full label after either dimension changes.
        window_class.style = CS_HREDRAW | CS_VREDRAW

        # Store the live ctypes callback Windows will invoke for messages.
        window_class.lpfnWndProc = self._window_proc_callback

        # GetModuleHandleW(None) returns the module of this running executable.
        window_class.hInstance = self._kernel32.GetModuleHandleW(None)

        # Without an explicit cursor, hovering the label leaves whatever pointer
        # the neighbouring window last set.
        window_class.hCursor = self._user32.LoadCursorW(None, IDC_ARROW)

        window_class.hbrBackground = self._background_brush_handle()

        # This exact name links registration to the later CreateWindowExW call.
        window_class.lpszClassName = CLASS_NAME

        # RegisterClassExW returns a zero atom on failure. A class already
        # registered by an earlier window in this process is not a failure.
        ctypes.set_last_error(0)
        if self._user32.RegisterClassExW(ctypes.byref(window_class)):
            return
        error = ctypes.get_last_error()
        if error != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(error)

    def _background_brush_handle(self) -> int:
        """Return the black erasing brush, creating it once for this process.

        After layered transparency is enabled, this same black becomes the
        transparent color that reveals the taskbar behind the label.
        """
        if self._background_brush is None:
            self._background_brush = self._gdi32.CreateSolidBrush(TRANSPARENT_COLORKEY)
        return self._background_brush

    # --- Styles and placement -------------------------------------------

    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool:
        """Convert the popup into a child window and attach it to Explorer.

        SetParent changes ownership but deliberately does not change window
        styles. Windows therefore requires us to clear ``popup`` and set
        ``child`` ourselves before attaching. If Explorer rejects the request,
        restore the original popup style so the controller can use its fallback.
        """
        # GetWindowLongPtrW returns the complete current style bitmask. Retain
        # it so a failed Explorer attachment can restore the popup exactly.
        original_style = self._user32.GetWindowLongPtrW(handle, GWL_STYLE)

        # Bit operations remove standalone-popup behavior and add child-window
        # behavior while leaving visibility and any unrelated flags untouched.
        child_style = (original_style & ~WS_POPUP) | WS_CHILD
        self._set_window_long(handle, GWL_STYLE, child_style)

        # SetParent can legitimately return zero when the previous parent was
        # null, and also returns zero on failure. Clearing LastError first lets
        # us distinguish those two outcomes after the call.
        ctypes.set_last_error(0)
        previous_parent = self._user32.SetParent(handle, taskbar)
        if not previous_parent and ctypes.get_last_error() != 0:
            # Explorer rejected the child. Restore popup behavior so the
            # controller can position it in absolute screen coordinates.
            self._set_window_long(handle, GWL_STYLE, original_style)
            return False

        self._apply_frame_change(handle)
        return True

    def _apply_frame_change(self, handle: int) -> None:
        """Make Windows notice a style change without moving or showing anything."""
        frame_flags = (
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        )
        # SetWindowPos with zero geometry plus NOMOVE/NOSIZE applies no visual
        # movement; FRAMECHANGED merely makes Windows notice the new style.
        if not self._user32.SetWindowPos(handle, None, 0, 0, 0, 0, frame_flags):
            raise ctypes.WinError(ctypes.get_last_error())

    def _set_window_long(self, handle: int, index: int, value: int) -> None:
        """Write one pointer-sized window field, handling Win32's ambiguous zero."""
        # SetWindowLongPtrW may validly return zero (when the old value was zero)
        # or return zero for failure, so LastError disambiguates the two.
        ctypes.set_last_error(0)
        previous_value = self._user32.SetWindowLongPtrW(handle, index, value)
        if previous_value == 0 and ctypes.get_last_error() != 0:
            raise ctypes.WinError(ctypes.get_last_error())

    def set_colorkey_transparency(self, handle: int) -> None:
        """Make the black background transparent, leaving only painted text."""
        # Read the existing extended flags so enabling layered rendering does
        # not erase TOOLWINDOW or NOACTIVATE behavior.
        extended_style = self._user32.GetWindowLongPtrW(handle, GWL_EXSTYLE)

        # Layered-window support is required before SetLayeredWindowAttributes
        # will accept a transparent color key.
        self._set_window_long(handle, GWL_EXSTYLE, extended_style | WS_EX_LAYERED)

        # SetLayeredWindowAttributes makes every black pixel fully transparent.
        # Alpha is zero because LWA_COLORKEY uses the color rather than alpha.
        if not self._user32.SetLayeredWindowAttributes(
            handle,
            TRANSPARENT_COLORKEY,
            0,
            LWA_COLORKEY,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None:
        """Move and resize the label without activating or changing visibility."""
        # Every move avoids activation. Embedded children preserve their normal
        # z-order; only the standalone fallback is promoted above the taskbar.
        flags = SWP_NOACTIVATE
        if not topmost:
            flags |= SWP_NOZORDER

        # SetWindowPos applies both location and size. Child coordinates are
        # taskbar-relative; fallback popup coordinates are screen-relative.
        if not self._user32.SetWindowPos(
            handle,
            HWND_TOPMOST if topmost else None,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            flags,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def set_visible(self, handle: int, visible: bool) -> None:
        """Show without taking focus, or hide the native label."""
        # ShowWindow changes visibility only; placement remains untouched.
        self._user32.ShowWindow(handle, SW_SHOWNOACTIVATE if visible else SW_HIDE)

    # --- Text and painting ----------------------------------------------

    def set_text(self, handle: int, text: str) -> None:
        """Store replacement text and ask Windows to repaint the label."""
        # Keep Python's paint source synchronized with the native window title.
        self._window_text = text

        # SetWindowTextW updates the native title. It does not guarantee our
        # custom WM_PAINT callback runs immediately, hence InvalidateRect below.
        if not self._user32.SetWindowTextW(handle, text):
            raise ctypes.WinError(ctypes.get_last_error())

        self._request_repaint(handle)

    def _request_repaint(self, handle: int) -> None:
        """Mark the whole label dirty so the next paint redraws it completely."""
        # The TRUE erase flag clears the old glyphs with the black background
        # brush registered for this window class.
        self._user32.InvalidateRect(handle, None, True)

    def refresh_theme(self, handle: int) -> None:
        """Re-read light/dark mode, repainting only if the text colour changed.

        Windows broadcasts WM_SETTINGCHANGE to top-level windows only, so once
        the label is a taskbar child the message never arrives and the
        controller polls this instead.
        """
        color = foreground_color_for_theme(uses_light_theme=system_uses_light_theme())
        if color == self._foreground_color:
            return
        self._foreground_color = color
        self._request_repaint(handle)

    def message_font(self) -> int | None:
        """Return the system UI font, creating it once from the current metrics.

        The stock GUI font is an 8pt legacy face that matches neither the
        taskbar's typeface nor the user's display scaling.
        """
        if self._font_resolved:
            return self._font_handle
        self._font_resolved = True

        metrics = NONCLIENTMETRICSW()
        metrics.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)
        queried = self._user32.SystemParametersInfoW(
            SPI_GETNONCLIENTMETRICS,
            ctypes.sizeof(NONCLIENTMETRICSW),
            ctypes.byref(metrics),
            0,
        )
        if queried:
            self._font_handle = self._gdi32.CreateFontIndirectW(
                ctypes.byref(metrics.lfMessageFont)
            )
        else:
            log.warning("unable to read system UI font metrics; using the stock font")
            self._font_handle = self._gdi32.GetStockObject(DEFAULT_GUI_FONT)
        return self._font_handle

    def _paint_label(self, hwnd: int) -> None:
        """Draw the current usage text centered in the label's client area."""
        # PAINTSTRUCT receives bookkeeping that must be passed back to EndPaint
        # after drawing finishes.
        paint = PAINTSTRUCT()

        # BeginPaint validates the dirty region and supplies a device
        # context—the native drawing surface used by GDI.
        device_context = self._user32.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            # GetClientRect returns the drawable interior using coordinates
            # relative to the label's own top-left corner.
            client_rect = wintypes.RECT()
            self._user32.GetClientRect(hwnd, ctypes.byref(client_rect))

            # Draw only text. The black background is removed by color-key
            # transparency, allowing the real taskbar to show through.
            self._gdi32.SetBkMode(device_context, TRANSPARENT_BACKGROUND)
            self._gdi32.SetTextColor(device_context, self._foreground_color)
            self._gdi32.SelectObject(device_context, self.message_font())

            # DrawTextW lays out the complete Python string (-1 means
            # null-terminated) inside client_rect using the centering flags.
            self._user32.DrawTextW(
                device_context,
                self._window_text,
                -1,
                ctypes.byref(client_rect),
                DT_CENTER | DT_VCENTER | DT_SINGLELINE,
            )
        finally:
            # Every successful BeginPaint must be paired with EndPaint so
            # Windows clears the dirty region and releases the drawing context.
            self._user32.EndPaint(hwnd, ctypes.byref(paint))

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Route Windows messages, handling only painting and theme changes."""
        if message == WM_PAINT:
            self._paint_label(hwnd)
            return 0

        if message in (WM_SETTINGCHANGE, WM_THEMECHANGED):
            # Only the unattached fallback popup ever receives these, but taking
            # them saves it waiting for the controller's slower poll.
            self.refresh_theme(hwnd)
            return 0

        # DefWindowProcW provides Windows' standard behavior for lifecycle,
        # sizing, cursor, and every other message ClaudeMonitor does not handle.
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # --- Message pump ----------------------------------------------------

    def pump_messages(
        self,
        stop_requested: threading.Event,
        duration_seconds: float,
    ) -> None:
        """Let Windows deliver paint events until refresh time or application exit."""
        # The monotonic deadline is immune to wall-clock/time-zone adjustments.
        deadline = time.monotonic() + duration_seconds

        # PeekMessageW overwrites this structure for each queued message.
        message = wintypes.MSG()

        # Event.wait doubles as a short sleep and a prompt shutdown signal.
        while not stop_requested.wait(_PUMP_POLL_SECONDS):
            # PeekMessageW returns nonzero while a queued message exists. Passing
            # no HWND and a zero range accepts every message for this UI thread.
            while self._user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, PM_REMOVE
            ):
                if message.message == WM_QUIT:
                    stop_requested.set()
                    return

                # TranslateMessage creates character messages from keyboard
                # input when relevant; it is harmless for paint-only traffic.
                self._user32.TranslateMessage(ctypes.byref(message))

                # DispatchMessageW invokes _window_proc for this window.
                self._user32.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= deadline:
                return
