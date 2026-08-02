"""Low-level Windows implementation for the taskbar usage label.

Most of ClaudeMonitor is ordinary Python. This module is the deliberately
isolated exception: it translates readable operations such as "move the label"
or "change its text" into calls to Windows' ``user32`` and ``gdi32`` DLLs.

The Windows vocabulary itself — constants, C structures, and function
signatures — lives in ``win32_bindings`` so this file describes only behavior.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PIL import Image

from .models import Rect
from .win32_bindings import (
    BI_RGB,
    BITMAPINFO,
    BITMAPINFOHEADER,
    CLASS_NAME,
    COMCTL32_SIGNATURES,
    CS_HREDRAW,
    CS_VREDRAW,
    DEFAULT_GUI_FONT,
    DIB_RGB_COLORS,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    DPI_AWARENESS_UNAWARE,
    DT_SINGLELINE,
    DT_VCENTER,
    ERROR_CLASS_ALREADY_EXISTS,
    GDI32_SIGNATURES,
    GW_CHILD,
    GW_HWNDNEXT,
    GWL_EXSTYLE,
    GWL_STYLE,
    HWND_TOPMOST,
    ICC_WIN95_CLASSES,
    IDC_ARROW,
    INITCOMMONCONTROLSEX,
    KERNEL32_SIGNATURES,
    LWA_COLORKEY,
    NONCLIENTMETRICSW,
    PAINTSTRUCT,
    PM_REMOVE,
    SIZE,
    SPI_GETNONCLIENTMETRICS,
    SRCCOPY,
    STRETCH_HALFTONE,
    SW_HIDE,
    SW_SHOWNOACTIVATE,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    TOOLINFOW,
    TOOLTIPS_CLASS,
    TTF_IDISHWND,
    TTF_TRACK,
    TTM_ADDTOOLW,
    TTM_SETMAXTIPWIDTH,
    TTM_SETTIPBKCOLOR,
    TTM_SETTIPTEXTCOLOR,
    TTM_TRACKACTIVATE,
    TTM_TRACKPOSITION,
    TTM_UPDATE,
    TTM_UPDATETIPTEXTW,
    TTS_ALWAYSTIP,
    TTS_NOPREFIX,
    TRANSPARENT_BACKGROUND,
    TRANSPARENT_COLORKEY,
    USER_DEFAULT_SCREEN_DPI,
    USER32_SIGNATURES,
    UXTHEME_SIGNATURES,
    WM_PAINT,
    WM_QUIT,
    WM_SETTINGCHANGE,
    WM_THEMECHANGED,
    WNDCLASSEXW,
    WNDPROC,
    WS_CHILD,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOPMOST,
    WS_EX_TOOLWINDOW,
    WS_POPUP,
    apply_signatures,
    foreground_color_for_theme,
    system_uses_light_theme,
)

log = logging.getLogger(__name__)

# How often the message pump checks for shutdown while waiting for messages.
_PUMP_POLL_SECONDS = 0.05

# Window classes are registered process-wide and never unregistered, so once a
# callback is handed to RegisterClassExW, Windows may invoke it for the rest of
# the process's life — even after the Win32TaskbarWindow instance that created
# it is garbage collected. Keeping every registering instance's callback here
# stops that trampoline from being freed out from under a later CreateWindowExW
# call, which otherwise crashes with an access violation.
_registered_wndproc_callbacks: list[object] = []

# The Claude glyph replaces the literal word "Claude" in the label, so it is
# drawn at the same square size as the tray's own status dot.
_ICON_SIZE = 16
_ICON_LEFT_INSET = 6
_ICON_TEXT_GAP = 6
# Breathing room after the text so it does not touch the taskbar's own icons.
_ICON_CONTENT_RIGHT_PADDING = 8
_ICON_ASSET_PATH = Path(__file__).parent / "assets" / "claude_icon.png"
_TOOLTIP_MAX_WIDTH = 600
_TOOLTIP_TASKBAR_GAP = 4
_TOOLTIP_BACKGROUND_COLOR = 0x002B2B2B
_TOOLTIP_TEXT_COLOR = 0x00F5F5F5


def scale_for_dpi(value: int, dpi: int) -> int:
    """Convert a constant written for 96 DPI into pixels for a display at ``dpi``."""
    # GetDpiForWindow answers 0 for a handle Windows no longer recognizes, and
    # a zero scale factor would collapse the label to nothing.
    if dpi <= 0:
        return value
    return round(value * dpi / USER_DEFAULT_SCREEN_DPI)


def _user32_for_dpi() -> Any:
    """Return a user32 handle with the DPI calls' argument types declared.

    This runs before ``Win32TaskbarWindow`` exists, because awareness has to be
    set before the process creates its first window — so it cannot borrow the
    adapter's already-prepared DLL. Declaring the signatures matters here more
    than anywhere else: without them ctypes passes the ``-4`` awareness context
    as a 32-bit int, and the truncated value silently fails to match any
    context Windows recognizes.
    """
    dll = ctypes.WinDLL("user32", use_last_error=True)
    apply_signatures(dll, USER32_SIGNATURES)
    return dll


def enable_per_monitor_dpi_awareness(user32: Any | None = None) -> bool:
    """Adopt the taskbar's DPI awareness, and report whether per-monitor was won.

    Explorer's taskbar is per-monitor aware. While ClaudeMonitor was unaware,
    Windows virtualized every coordinate crossing between the two, so a slot
    requested as 180x48 was applied as 144x38 on a 125% display and the label
    both mis-sized itself and kept its old scale after moving to a second
    monitor. Must be called before any window is created.
    """
    dll = _user32_for_dpi() if user32 is None else user32

    # SetProcessDpiAwarenessContext is Windows 10 1703 and later. It also fails
    # when awareness was already established, which is not worth dying over in
    # the first statement of the program.
    try:
        if dll.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return True
    except (AttributeError, OSError) as exc:
        log.info("per-monitor DPI awareness unavailable (%s); trying system DPI", exc)

    # Older builds offer only one process-wide, system-DPI setting. That still
    # stops coordinates being virtualized on a single-monitor machine.
    try:
        dll.SetProcessDPIAware()
    except (AttributeError, OSError) as exc:
        log.warning("unable to declare any DPI awareness (%s)", exc)
    return False


def process_dpi_awareness(user32: Any | None = None) -> int:
    """Return this process's DPI awareness using the same scale as a window's."""
    dll = _user32_for_dpi() if user32 is None else user32
    try:
        # A thread with no explicit context reports the process default, so the
        # current thread's context is the process's answer.
        context = dll.GetThreadDpiAwarenessContext()
        return dll.GetAwarenessFromDpiAwarenessContext(context)
    except (AttributeError, OSError):
        return DPI_AWARENESS_UNAWARE


@functools.lru_cache(maxsize=1)
def _load_claude_icon() -> Image.Image:
    """Load the bundled Claude glyph once and reuse it for every paint."""
    return Image.open(_ICON_ASSET_PATH).convert("RGBA")


def _icon_bgr_bytes(image: Image.Image) -> bytes:
    """Pack an RGBA image as the row-padded, top-down 24bpp BGR buffer
    ``SetDIBitsToDevice`` expects.

    Compositing onto opaque black before dropping the alpha channel means
    every pixel outside the glyph becomes exactly the window's color-keyed
    background, so it disappears rather than leaving a dark halo.
    """
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 255))
    opaque = Image.alpha_composite(canvas, image).convert("RGB")
    red, green, blue = opaque.split()
    bgr_rows = Image.merge("RGB", (blue, green, red)).tobytes()

    width, _height = image.size
    row_bytes = width * 3
    padded_row_bytes = (row_bytes + 3) & ~3
    if padded_row_bytes == row_bytes:
        return bgr_rows

    padding = b"\x00" * (padded_row_bytes - row_bytes)
    rows = (
        bgr_rows[offset : offset + row_bytes] + padding
        for offset in range(0, len(bgr_rows), row_bytes)
    )
    return b"".join(rows)


def _bitmap_info_for(*, width: int, height: int) -> BITMAPINFO:
    """Describe an uncompressed, top-down 24bpp DIB of the given size."""
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    # Negative height selects top-down row order, matching the PNG's own.
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 24
    info.bmiHeader.biCompression = BI_RGB
    return info


def _initialize_tooltip_controls(comctl32: Any) -> None:
    """Register Windows' standard tooltip class for this process."""
    controls = INITCOMMONCONTROLSEX()
    controls.dwSize = ctypes.sizeof(INITCOMMONCONTROLSEX)
    controls.dwICC = ICC_WIN95_CLASSES
    if not comctl32.InitCommonControlsEx(ctypes.byref(controls)):
        raise OSError("unable to initialize Windows tooltip controls")


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
        self._comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self._uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)

        missing = (
            apply_signatures(self._user32, USER32_SIGNATURES)
            + apply_signatures(self._gdi32, GDI32_SIGNATURES)
            + apply_signatures(self._kernel32, KERNEL32_SIGNATURES)
            + apply_signatures(self._comctl32, COMCTL32_SIGNATURES)
            + apply_signatures(self._uxtheme, UXTHEME_SIGNATURES)
        )
        if missing:
            # Every entry the label actually depends on has shipped since
            # Windows XP, so this is diagnostic rather than fatal.
            log.warning("Windows does not export %s; continuing without it", missing)

        _initialize_tooltip_controls(self._comctl32)

        # Keep the callback object alive as long as Windows may call it. If it
        # were garbage-collected, a later paint message could crash the process.
        self._window_proc_callback = WNDPROC(self._window_proc)

        # DrawTextW reads this latest value whenever Windows sends WM_PAINT.
        self._window_text = ""

        # GDI objects are created once per process and shared by every window
        # this adapter registers, so recreating the label never leaks handles.
        # The font is the exception: it is sized for one display scale, so the
        # DPI it was built for is remembered and it is rebuilt when the taskbar
        # moves to a monitor that scales differently. A resolved font may
        # legitimately be None (a NULL handle), so the recorded DPI rather than
        # the value marks that the lookup already happened.
        self._font_handle: int | None = None
        self._font_dpi: int | None = None
        self._font_is_stock = False

        # The label whose monitor decides the scale for every measurement. It
        # exists only between create_window and close_window.
        self._label_handle: int | None = None
        self._background_brush: int | None = None
        self._uses_light_theme = system_uses_light_theme()
        self._foreground_color = foreground_color_for_theme(
            uses_light_theme=self._uses_light_theme
        )

        # The icon's pixel buffer never changes at runtime, so it is packed
        # once and reused for every WM_PAINT rather than re-composited each time.
        self._icon_bytes: bytes | None = None
        self._icon_info: BITMAPINFO | None = None

        # Each tooltip control and its backing Unicode buffer must stay alive
        # for as long as the associated taskbar label exists.
        self._tooltip_handles: dict[int, int] = {}
        self._tooltip_buffers: dict[int, ctypes.Array] = {}
        self._active_tooltip_labels: set[int] = set()

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

        # Every later measurement asks this window which display it is on, so
        # the label follows the taskbar's scaling rather than the primary
        # monitor's.
        self._label_handle = handle
        return handle

    def close_window(self, handle: int) -> None:
        """Destroy the label on the same thread that created it."""
        tooltip_handle = self._tooltip_handles.pop(handle, None)
        self._tooltip_buffers.pop(handle, None)
        self._active_tooltip_labels.discard(handle)
        if tooltip_handle is not None and self._user32.IsWindow(tooltip_handle):
            self._user32.DestroyWindow(tooltip_handle)

        # A destroyed window can no longer be asked about its display, so stop
        # measuring against it; the unscaled default applies until the
        # controller rebuilds the label.
        if self._label_handle == handle:
            self._label_handle = None

        # IsWindow protects cleanup from a stale handle if Explorer or Windows
        # already destroyed the native label.
        if self._user32.IsWindow(handle):
            # DestroyWindow releases the HWND and sends final destruction
            # messages. Calling it on the creating thread satisfies Win32 rules.
            self._user32.DestroyWindow(handle)

    def _register_class(self) -> None:
        """Teach Windows how to create and repaint this process's label windows."""
        self._register_window_class(
            class_name=CLASS_NAME,
            background_brush=self._background_brush_handle(),
        )

    def _register_window_class(
        self,
        *,
        class_name: str,
        background_brush: int | None,
    ) -> None:
        """Register one custom window class backed by this adapter's callback."""
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

        window_class.hbrBackground = background_brush

        # This exact name links registration to the later CreateWindowExW call.
        window_class.lpszClassName = class_name

        # RegisterClassExW returns a zero atom on failure. A class already
        # registered by an earlier window in this process is not a failure.
        ctypes.set_last_error(0)
        if self._user32.RegisterClassExW(ctypes.byref(window_class)):
            # This instance's callback is now the one Windows calls for every
            # window of this class, for as long as the process runs.
            if self._window_proc_callback not in _registered_wndproc_callbacks:
                _registered_wndproc_callbacks.append(self._window_proc_callback)
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
        if not visible:
            self._hide_tooltip(handle)
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

    def set_tooltip(self, handle: int, tooltip: str) -> None:
        """Attach or update the multiline hover detail for a taskbar label."""
        tooltip_handle = self._tooltip_handles.get(handle)
        tooltip_is_active = handle in self._active_tooltip_labels

        buffer = ctypes.create_unicode_buffer(tooltip)
        tool_info = TOOLINFOW()
        tool_info.cbSize = ctypes.sizeof(TOOLINFOW)
        tool_info.uFlags = TTF_IDISHWND | TTF_TRACK
        tool_info.hwnd = handle
        tool_info.uId = handle
        tool_info.lpszText = ctypes.cast(buffer, wintypes.LPWSTR)
        info_pointer = ctypes.cast(ctypes.byref(tool_info), ctypes.c_void_p).value

        if tooltip_handle is None:
            tooltip_handle = self._user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                TOOLTIPS_CLASS,
                None,
                WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX,
                0,
                0,
                0,
                0,
                handle,
                None,
                self._kernel32.GetModuleHandleW(None),
                None,
            )
            if not tooltip_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._configure_tooltip_theme(tooltip_handle)
            self._user32.SendMessageW(
                tooltip_handle, TTM_SETMAXTIPWIDTH, 0, _TOOLTIP_MAX_WIDTH
            )
            if not self._user32.SendMessageW(
                tooltip_handle, TTM_ADDTOOLW, 0, info_pointer
            ):
                self._user32.DestroyWindow(tooltip_handle)
                raise ctypes.WinError(ctypes.get_last_error())
            self._tooltip_handles[handle] = tooltip_handle
        else:
            self._user32.SendMessageW(
                tooltip_handle, TTM_UPDATETIPTEXTW, 0, info_pointer
            )

        # The tooltip control keeps this pointer rather than copying its text.
        self._tooltip_buffers[handle] = buffer
        if tooltip_is_active:
            # Redraw the existing tracking popup in place. TRACKACTIVATE is not
            # repeated, so the once-per-second age update cannot restart its
            # hover lifecycle or make the popup disappear and reappear.
            self._user32.SendMessageW(tooltip_handle, TTM_UPDATE, 0, 0)
            try:
                label_rect = self.get_rect(handle)
            except OSError:
                return
            self._position_tooltip_above_label(
                label_rect=label_rect,
                tooltip_handle=tooltip_handle,
            )

    def _configure_tooltip_theme(self, tooltip_handle: int) -> None:
        """Ask Windows to render the tooltip with Explorer's current styling."""
        theme_name = "Explorer" if self._uses_light_theme else "DarkMode_Explorer"
        if self._uxtheme.SetWindowTheme(tooltip_handle, theme_name, None) == 0:
            return
        if self._uses_light_theme:
            # The default common-control theme is already an appropriate light
            # fallback when this Windows build does not expose Explorer's theme.
            return

        # Older builds can reject the dark Explorer theme. Tooltip color
        # messages are ignored while visual styles remain enabled, so disable
        # them only for this fallback and retain readable light-on-dark colors.
        self._uxtheme.SetWindowTheme(tooltip_handle, "", "")
        self._user32.SendMessageW(
            tooltip_handle, TTM_SETTIPBKCOLOR, _TOOLTIP_BACKGROUND_COLOR, 0
        )
        self._user32.SendMessageW(
            tooltip_handle, TTM_SETTIPTEXTCOLOR, _TOOLTIP_TEXT_COLOR, 0
        )

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
        uses_light_theme = system_uses_light_theme()
        color = foreground_color_for_theme(uses_light_theme=uses_light_theme)
        if color == self._foreground_color:
            return
        self._uses_light_theme = uses_light_theme
        self._foreground_color = color
        self._request_repaint(handle)
        tooltip_handle = self._tooltip_handles.get(handle)
        if tooltip_handle is not None:
            self._configure_tooltip_theme(tooltip_handle)

    def label_dpi(self) -> int:
        """Return the scaling of the display the label currently sits on.

        Asking the window rather than the system is what lets the label follow
        the taskbar onto a second monitor with different scaling instead of
        keeping the DPI it happened to be born on.
        """
        if self._label_handle is None:
            return USER_DEFAULT_SCREEN_DPI
        try:
            dpi = self._user32.GetDpiForWindow(self._label_handle)
        except (AttributeError, OSError):
            return USER_DEFAULT_SCREEN_DPI
        return dpi or USER_DEFAULT_SCREEN_DPI

    def window_dpi_awareness(self, handle: int) -> int:
        """Return the DPI awareness Windows records for another window."""
        try:
            context = self._user32.GetWindowDpiAwarenessContext(handle)
            return self._user32.GetAwarenessFromDpiAwarenessContext(context)
        except (AttributeError, OSError):
            return DPI_AWARENESS_UNAWARE

    def _read_font_metrics(self, dpi: int) -> NONCLIENTMETRICSW | None:
        """Read the system UI font metrics as they apply at ``dpi``."""
        metrics = NONCLIENTMETRICSW()
        metrics.cbSize = ctypes.sizeof(NONCLIENTMETRICSW)

        # SystemParametersInfoForDpi (Windows 10 1607) is the only variant that
        # answers for a display other than the one Windows considers primary.
        try:
            queried = self._user32.SystemParametersInfoForDpi(
                SPI_GETNONCLIENTMETRICS,
                ctypes.sizeof(NONCLIENTMETRICSW),
                ctypes.byref(metrics),
                0,
                dpi,
            )
        except (AttributeError, OSError):
            queried = 0

        # It refuses by returning zero rather than raising, and the stock font
        # is a visibly different typeface, so an unexplained refusal is worth
        # one more attempt at the system-wide metrics before giving that up.
        if not queried:
            queried = self._user32.SystemParametersInfoW(
                SPI_GETNONCLIENTMETRICS,
                ctypes.sizeof(NONCLIENTMETRICSW),
                ctypes.byref(metrics),
                0,
            )
        return metrics if queried else None

    def message_font(self, dpi: int | None = None) -> int | None:
        """Return the system UI font sized for the display the label is on.

        The stock GUI font is an 8pt legacy face that matches neither the
        taskbar's typeface nor the user's display scaling. The font is cached
        per DPI: reusing one built for a 125% laptop screen on a 100% external
        monitor is what left the label visibly oversized after docking.
        """
        dpi = self.label_dpi() if dpi is None else dpi
        if self._font_dpi == dpi:
            return self._font_handle

        metrics = self._read_font_metrics(dpi)
        if metrics is not None:
            replacement = self._gdi32.CreateFontIndirectW(
                ctypes.byref(metrics.lfMessageFont)
            )
            replacement_is_stock = False
        else:
            log.warning("unable to read system UI font metrics; using the stock font")
            replacement = self._gdi32.GetStockObject(DEFAULT_GUI_FONT)
            replacement_is_stock = True

        self._release_font()
        self._font_handle = replacement
        self._font_is_stock = replacement_is_stock
        self._font_dpi = dpi
        return self._font_handle

    def _release_font(self) -> None:
        """Free the font built for a previous display scale.

        Every rescale creates a new GDI object, and the process is long-lived,
        so the superseded one has to go back to Windows. A stock object is
        owned by Windows and is the one kind that must be left alone.
        """
        if self._font_handle is None or self._font_dpi is None:
            return
        if not self._font_is_stock:
            try:
                self._gdi32.DeleteObject(self._font_handle)
            except (AttributeError, OSError) as exc:
                log.warning("unable to release the previous label font (%s)", exc)
        self._font_handle = None

    def measure_text_width(self, text: str) -> int:
        """Return the pixel width the message font renders this text at.

        A memory device context needs no window of its own, so this can be
        called before the label exists to size it to its very first text.
        """
        device_context = self._gdi32.CreateCompatibleDC(None)
        try:
            self._gdi32.SelectObject(device_context, self.message_font())
            size = SIZE()
            self._gdi32.GetTextExtentPoint32W(
                device_context, text, len(text), ctypes.byref(size)
            )
            return size.cx
        finally:
            self._gdi32.DeleteDC(device_context)

    def content_width_for(self, text: str) -> int:
        """Return the label width that fits the icon and this text exactly,
        so short strings never leave dead space before the notification area.

        The text is measured with a font Windows already sized for this
        display, but the insets around it are plain constants and have to be
        scaled here or they shrink relative to the text as scaling rises.
        """
        dpi = self.label_dpi()
        return (
            scale_for_dpi(_ICON_LEFT_INSET, dpi)
            + scale_for_dpi(_ICON_SIZE, dpi)
            + scale_for_dpi(_ICON_TEXT_GAP, dpi)
            + self.measure_text_width(text)
            + scale_for_dpi(_ICON_CONTENT_RIGHT_PADDING, dpi)
        )

    def _icon_pixels(self) -> tuple[bytes, BITMAPINFO]:
        """Return the cached BGR pixel buffer and DIB header for the Claude glyph."""
        if self._icon_bytes is None or self._icon_info is None:
            image = _load_claude_icon()
            self._icon_bytes = _icon_bgr_bytes(image)
            self._icon_info = _bitmap_info_for(width=image.width, height=image.height)
        return self._icon_bytes, self._icon_info

    def _draw_icon(
        self, device_context: int, client_rect: wintypes.RECT, dpi: int
    ) -> None:
        """Blit the Claude glyph against the label's left edge, vertically centered.

        The glyph is stretched rather than copied pixel for pixel, because a
        fixed 16px square shrinks to a speck beside text that Windows has
        already scaled up for a 125% or 150% display.
        """
        pixels, bitmap_info = self._icon_pixels()
        source_size = _load_claude_icon().width
        drawn_size = scale_for_dpi(_ICON_SIZE, dpi)
        icon_top = client_rect.top + (
            (client_rect.bottom - client_rect.top - drawn_size) // 2
        )

        # HALFTONE averages the source pixels instead of dropping them, which
        # is what keeps the glyph from looking ragged at fractional scales.
        self._gdi32.SetStretchBltMode(device_context, STRETCH_HALFTONE)
        self._gdi32.StretchDIBits(
            device_context,
            client_rect.left + scale_for_dpi(_ICON_LEFT_INSET, dpi),
            icon_top,
            drawn_size,
            drawn_size,
            0,
            0,
            source_size,
            source_size,
            pixels,
            ctypes.byref(bitmap_info),
            DIB_RGB_COLORS,
            SRCCOPY,
        )

    def _paint_label(self, hwnd: int) -> None:
        """Draw the Claude glyph and the current usage text in the label's
        client area, the glyph anchored left and the text left-aligned beside it."""
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

            # The black background is removed by color-key transparency,
            # allowing the real taskbar to show through both the icon and text.
            # One DPI reading drives the whole paint, so the glyph and the text
            # cannot disagree about the scale midway through drawing.
            dpi = self.label_dpi()

            self._gdi32.SetBkMode(device_context, TRANSPARENT_BACKGROUND)
            self._gdi32.SetTextColor(device_context, self._foreground_color)
            self._gdi32.SelectObject(device_context, self.message_font(dpi))

            self._draw_icon(device_context, client_rect, dpi)

            # The text starts right after the icon rather than centering across
            # the whole remaining width, which otherwise reads as a large gap
            # between the glyph and short strings like "100% (not started)".
            text_rect = wintypes.RECT(
                client_rect.left
                + scale_for_dpi(_ICON_LEFT_INSET + _ICON_SIZE + _ICON_TEXT_GAP, dpi),
                client_rect.top,
                client_rect.right,
                client_rect.bottom,
            )
            self._user32.DrawTextW(
                device_context,
                self._window_text,
                -1,
                ctypes.byref(text_rect),
                DT_VCENTER | DT_SINGLELINE,
            )
        finally:
            # Every successful BeginPaint must be paired with EndPaint so
            # Windows clears the dirty region and releases the drawing context.
            self._user32.EndPaint(hwnd, ctypes.byref(paint))

    def _poll_tooltip_hover(self) -> None:
        """Open the label tooltip while the cursor is inside its full rectangle."""
        cursor = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(cursor)):
            return

        for label_handle, tooltip_handle in list(self._tooltip_handles.items()):
            try:
                label_rect = self.get_rect(label_handle)
            except OSError:
                self._hide_tooltip(label_handle)
                continue

            cursor_is_inside = (
                label_rect.left <= cursor.x < label_rect.right
                and label_rect.top <= cursor.y < label_rect.bottom
            )
            if not cursor_is_inside:
                self._hide_tooltip(label_handle)
                continue
            if label_handle in self._active_tooltip_labels:
                continue

            # Tracking mode is designed for controls that supply their own
            # hover detection. It also works in the color-keyed gaps where the
            # layered label itself never receives ordinary mouse messages.
            label_center = label_rect.left + (label_rect.width // 2)
            position = (
                ((label_rect.top & 0xFFFF) << 16) | (label_center & 0xFFFF)
            )
            self._user32.SendMessageW(
                tooltip_handle,
                TTM_TRACKPOSITION,
                0,
                position,
            )
            tool_info = self._tracking_tool_info(label_handle)
            info_pointer = ctypes.cast(ctypes.byref(tool_info), ctypes.c_void_p).value
            self._user32.SendMessageW(
                tooltip_handle,
                TTM_TRACKACTIVATE,
                True,
                info_pointer,
            )
            self._position_tooltip_above_label(
                label_rect=label_rect,
                tooltip_handle=tooltip_handle,
            )
            self._active_tooltip_labels.add(label_handle)

    def _position_tooltip_above_label(
        self,
        *,
        label_rect: Rect,
        tooltip_handle: int,
    ) -> None:
        """Center the open tooltip immediately above its taskbar label."""
        try:
            tooltip_rect = self.get_rect(tooltip_handle)
        except OSError:
            return

        left = label_rect.left + ((label_rect.width - tooltip_rect.width) // 2)
        top = label_rect.top - tooltip_rect.height - _TOOLTIP_TASKBAR_GAP
        self._user32.SetWindowPos(
            tooltip_handle,
            HWND_TOPMOST,
            left,
            top,
            0,
            0,
            SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def _hide_tooltip(self, label_handle: int) -> None:
        """Close an explicitly opened tooltip once its label is no longer hovered."""
        if label_handle not in self._active_tooltip_labels:
            return
        self._active_tooltip_labels.discard(label_handle)
        tooltip_handle = self._tooltip_handles.get(label_handle)
        if tooltip_handle is not None:
            tool_info = self._tracking_tool_info(label_handle)
            info_pointer = ctypes.cast(ctypes.byref(tool_info), ctypes.c_void_p).value
            self._user32.SendMessageW(
                tooltip_handle,
                TTM_TRACKACTIVATE,
                False,
                info_pointer,
            )

    def _tracking_tool_info(self, label_handle: int) -> TOOLINFOW:
        """Build the matching TOOLINFO identifier used by tracking messages."""
        tool_info = TOOLINFOW()
        tool_info.cbSize = ctypes.sizeof(TOOLINFOW)
        tool_info.uFlags = TTF_IDISHWND | TTF_TRACK
        tool_info.hwnd = label_handle
        tool_info.uId = label_handle
        return tool_info

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
            self._poll_tooltip_hover()
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
