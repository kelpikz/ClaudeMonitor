"""Low-level Windows implementation for the taskbar usage label.

Most of ClaudeMonitor is ordinary Python. This module is the deliberately
isolated exception: it translates readable operations such as "move the label"
or "change its text" into calls to Windows' ``user32`` and ``gdi32`` DLLs.

The numeric constants below are values defined by the Windows API. They are
grouped by purpose so maintainers do not need to know the API vocabulary to
follow the higher-level controller in ``taskbar_companion.py``.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .taskbar_companion import Rect


# Basic window behavior: begin as a standalone popup, optionally visible, then
# convert to a child after Explorer accepts it into the taskbar.
_WS_POPUP = 0x80000000  # Create a top-level window before taskbar attachment.
_WS_CHILD = 0x40000000  # Make coordinates and lifetime belong to the taskbar.
_WS_VISIBLE = 0x10000000  # Ask Windows to display the window immediately.
_WS_EX_TOOLWINDOW = 0x00000080  # Keep the helper out of Alt+Tab.
_WS_EX_NOACTIVATE = 0x08000000  # Never steal keyboard focus.
_GWL_STYLE = -16  # Select the ordinary style field in Get/SetWindowLongPtr.
_GWL_EXSTYLE = -20  # Select the extended-style field in Get/SetWindowLongPtr.

# Transparency: pixels painted black become holes through which the taskbar's
# own acrylic background remains visible.
_WS_EX_LAYERED = 0x00080000  # Allow per-pixel transparency configuration.
_LWA_COLORKEY = 0x00000001  # Treat one chosen color as fully transparent.
_TRANSPARENT_COLORKEY = 0x00000000  # Black pixels will reveal the taskbar.

# Repositioning flags. Moving must not activate or accidentally show a window;
# visibility is controlled separately through ShowWindow.
_SWP_NOSIZE = 0x0001  # Preserve width and height during a style-only update.
_SWP_NOMOVE = 0x0002  # Preserve x and y during a style-only update.
_SWP_NOZORDER = 0x0004  # Preserve stacking order relative to other windows.
_SWP_NOACTIVATE = 0x0010  # Do not move keyboard focus to this window.
_SWP_FRAMECHANGED = 0x0020  # Recalculate the frame after changing styles.
_HWND_TOPMOST = -1  # Place the fallback popup above other normal windows.

# Windows sends messages to request painting and shutdown. PeekMessage removes
# each message from the queue before it is dispatched.
_WM_PAINT = 0x000F  # Windows is asking the window to redraw itself.
_WM_QUIT = 0x0012  # The thread's message loop should end.
_PM_REMOVE = 0x0001  # Remove messages as PeekMessage reads them.

# Text drawing options: center one line both horizontally and vertically, draw
# without a background rectangle, and use Windows' standard interface font.
_DT_CENTER = 0x00000001  # Center text horizontally.
_DT_VCENTER = 0x00000004  # Center text vertically.
_DT_SINGLELINE = 0x00000020  # Keep the usage summary on one line.
_TRANSPARENT = 1  # Do not let GDI paint a background behind glyphs.
_DEFAULT_GUI_FONT = 17  # Windows stock font identifier for standard UI text.
_TASKBAR_FOREGROUND = 0x00F5F5F5  # Near-white COLORREF in BGR byte order.

# Window-class and sibling-enumeration values used to register our label and
# walk the taskbar's direct child windows.
_CS_HREDRAW = 0x0002  # Repaint after horizontal resizing.
_CS_VREDRAW = 0x0001  # Repaint after vertical resizing.
_CLASS_NAME = "ClaudeMonitorTaskbarWindow"  # Process-local window type name.
_GW_HWNDNEXT = 2  # Continue to the next sibling window.
_GW_CHILD = 5  # Start at a parent's first child window.


# A window procedure is the callback Windows invokes whenever our label needs
# to paint or receives another operating-system message.
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT: pointer-sized value returned to Windows.
    wintypes.HWND,  # HWND: window receiving the message.
    wintypes.UINT,  # UINT: numeric message identifier such as WM_PAINT.
    wintypes.WPARAM,  # WPARAM: message-specific pointer-sized input.
    wintypes.LPARAM,  # LPARAM: second message-specific pointer-sized input.
)


class _WNDCLASSEXW(ctypes.Structure):
    """Python layout of the Win32 structure used to register a window type."""

    _fields_ = [
        ("cbSize", wintypes.UINT),  # Byte size, used for versioning the structure.
        ("style", wintypes.UINT),  # Redraw behavior shared by every window.
        ("lpfnWndProc", _WNDPROC),  # Callback that handles Windows messages.
        ("cbClsExtra", ctypes.c_int),  # Extra class bytes; ClaudeMonitor needs none.
        ("cbWndExtra", ctypes.c_int),  # Extra per-window bytes; also unused.
        ("hInstance", wintypes.HINSTANCE),  # Module that owns this window class.
        ("hIcon", wintypes.HICON),  # Large icon; omitted for the taskbar label.
        ("hCursor", wintypes.HANDLE),  # Mouse cursor; no custom cursor is needed.
        ("hbrBackground", wintypes.HBRUSH),  # Brush used to erase the background.
        ("lpszMenuName", wintypes.LPCWSTR),  # Native menu resource; none is attached.
        ("lpszClassName", wintypes.LPCWSTR),  # Name passed to CreateWindowExW.
        ("hIconSm", wintypes.HICON),  # Small icon; omitted for the taskbar label.
    ]


class _PAINTSTRUCT(ctypes.Structure):
    """Python layout of the drawing information Windows supplies while painting."""

    _fields_ = [
        ("hdc", wintypes.HDC),  # Drawing context prepared by BeginPaint.
        ("fErase", wintypes.BOOL),  # Whether Windows erased the background.
        ("rcPaint", wintypes.RECT),  # Region that needs repainting.
        ("fRestore", wintypes.BOOL),  # Reserved Windows bookkeeping value.
        ("fIncUpdate", wintypes.BOOL),  # Reserved Windows bookkeeping value.
        ("rgbReserved", ctypes.c_byte * 32),  # Private state owned by Windows.
    ]


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

        # Declare parameter and return types before making any DLL calls. This
        # prevents ctypes from truncating 64-bit window handles into C integers.
        self._configure_functions()

        # Keep the callback object alive as long as Windows may call it. If it
        # were garbage-collected, a later paint message could crash the process.
        self._window_proc_callback = _WNDPROC(self._window_proc)

        # Registration happens lazily before the first window is created, and
        # only once because duplicate class registration is an error.
        self._class_registered = False

        # DrawTextW reads this latest value whenever Windows sends WM_PAINT.
        self._window_text = ""

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

    def create_window(self, *, text: str, visible: bool) -> int:
        """Create a non-activating popup that can later be embedded in Explorer."""
        # CreateWindowExW can only use a class after RegisterClassExW has taught
        # Windows its name, paint callback, and background brush.
        self._register_class()

        # Preserve the same initial value locally because WM_PAINT reads from
        # this Python field rather than querying Windows for the title.
        self._window_text = text

        # The ordinary style controls parent/visibility behavior. Extended
        # styles keep this helper out of Alt+Tab and prevent focus stealing.
        style = _WS_POPUP | (_WS_VISIBLE if visible else 0)
        extended_style = _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE

        # GetModuleHandleW(None) identifies this running executable, which must
        # match the module used when the custom window class was registered.
        module_handle = self._kernel32.GetModuleHandleW(None)

        # CreateWindowExW returns the HWND used by every later operation. The
        # initial 1x1 size avoids flashing a full-sized popup before placement;
        # no parent/menu/creation payload is needed at this stage.
        handle = self._user32.CreateWindowExW(
            extended_style,
            _CLASS_NAME,
            text,
            style,
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

    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool:
        """Convert the popup into a child window and attach it to Explorer.

        SetParent changes ownership but deliberately does not change window
        styles. Windows therefore requires us to clear ``popup`` and set
        ``child`` ourselves before attaching. If Explorer rejects the request,
        restore the original popup style so the controller can use its fallback.
        """
        # GetWindowLongPtrW returns the complete current style bitmask. Retain
        # it so a failed Explorer attachment can restore the popup exactly.
        original_style = self._user32.GetWindowLongPtrW(handle, _GWL_STYLE)

        # Bit operations remove standalone-popup behavior and add child-window
        # behavior while leaving visibility and any unrelated flags untouched.
        child_style = (original_style & ~_WS_POPUP) | _WS_CHILD
        self._set_window_style(handle, child_style)

        # SetParent can legitimately return zero when the previous parent was
        # null, and also returns zero on failure. Clearing LastError first lets
        # us distinguish those two outcomes after the call.
        ctypes.set_last_error(0)
        previous_parent = self._user32.SetParent(handle, taskbar)
        if not previous_parent and ctypes.get_last_error() != 0:
            # Explorer rejected the child. Restore popup behavior so the
            # controller can position it in absolute screen coordinates.
            self._set_window_style(handle, original_style)
            return False

        # Tell Windows to recalculate the non-client frame after the style
        # change without moving, resizing, activating, or reordering the label.
        frame_flags = (
            _SWP_NOMOVE
            | _SWP_NOSIZE
            | _SWP_NOZORDER
            | _SWP_NOACTIVATE
            | _SWP_FRAMECHANGED
        )
        # SetWindowPos with zero geometry plus NOMOVE/NOSIZE applies no visual
        # movement; FRAMECHANGED merely makes Windows notice the new style.
        if not self._user32.SetWindowPos(handle, None, 0, 0, 0, 0, frame_flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return True

    def _set_window_style(self, handle: int, style: int) -> None:
        """Apply a base window style while handling Win32's ambiguous zero result."""
        # Like SetParent, SetWindowLongPtrW may validly return zero (when the old
        # value was zero) or return zero for failure, so LastError disambiguates.
        ctypes.set_last_error(0)
        previous_style = self._user32.SetWindowLongPtrW(handle, _GWL_STYLE, style)
        if previous_style == 0 and ctypes.get_last_error() != 0:
            raise ctypes.WinError(ctypes.get_last_error())

    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]:
        """Collect visible windows sharing the taskbar so placement can avoid them."""
        # Accumulate plain Python rectangles; the controller decides which
        # adjacent rectangles actually affect placement.
        rects: list[Rect] = []

        # GetWindow(GW_CHILD) starts at the first direct taskbar child. Repeated
        # GW_HWNDNEXT calls then walk its siblings without recursion.
        child = self._user32.GetWindow(taskbar, _GW_CHILD)
        while child:
            # Ignore our own HWND and hidden Explorer helpers. IsWindowVisible
            # reports the effective WS_VISIBLE state of each sibling.
            if child != exclude_handle and self._user32.IsWindowVisible(child):
                rect = self.get_rect(child)
                if rect.width > 0 and rect.height > 0:
                    rects.append(rect)

            # Advance to the next sibling; a zero return ends enumeration.
            child = self._user32.GetWindow(child, _GW_HWNDNEXT)
        return rects

    def set_colorkey_transparency(self, handle: int) -> None:
        """Make the black background transparent, leaving only painted text."""
        # Read the existing extended flags so enabling layered rendering does
        # not erase TOOLWINDOW or NOACTIVATE behavior.
        extended_style = self._user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)

        # SetWindowLongPtrW turns on layered-window support, which is required
        # before SetLayeredWindowAttributes accepts a transparent color key.
        self._user32.SetWindowLongPtrW(
            handle,
            _GWL_EXSTYLE,
            extended_style | _WS_EX_LAYERED,
        )

        # SetLayeredWindowAttributes makes every black pixel fully transparent.
        # Alpha is zero because LWA_COLORKEY uses the color rather than alpha.
        if not self._user32.SetLayeredWindowAttributes(
            handle,
            _TRANSPARENT_COLORKEY,
            0,
            _LWA_COLORKEY,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None:
        """Move and resize the label without activating or changing visibility."""
        # Every move avoids activation. Embedded children preserve their normal
        # z-order; only the standalone fallback is promoted above the taskbar.
        flags = _SWP_NOACTIVATE
        if not topmost:
            flags |= _SWP_NOZORDER

        # SetWindowPos applies both location and size. Child coordinates are
        # taskbar-relative; fallback popup coordinates are screen-relative.
        if not self._user32.SetWindowPos(
            handle,
            _HWND_TOPMOST if topmost else None,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            flags,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def set_text(self, handle: int, text: str) -> None:
        """Store replacement text and ask Windows to repaint the label."""
        # Keep Python's paint source synchronized with the native window title.
        self._window_text = text

        # SetWindowTextW updates the native title. It does not guarantee our
        # custom WM_PAINT callback runs immediately, hence InvalidateRect below.
        if not self._user32.SetWindowTextW(handle, text):
            raise ctypes.WinError(ctypes.get_last_error())

        # InvalidateRect marks the entire client area dirty. The TRUE erase flag
        # clears the old glyphs with the registered black background brush.
        self._user32.InvalidateRect(handle, None, True)

    def set_visible(self, handle: int, visible: bool) -> None:
        """Show without taking focus, or hide the native label."""
        # These are ShowWindow command values: SW_SHOWNOACTIVATE and SW_HIDE.
        show_without_activation = 8
        hide = 0

        # ShowWindow changes visibility only; placement remains untouched.
        self._user32.ShowWindow(handle, show_without_activation if visible else hide)

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

        # Event.wait doubles as a 50 ms sleep and a prompt shutdown signal.
        while not stop_requested.wait(0.05):
            # PeekMessageW returns nonzero while a queued message exists. Passing
            # no HWND and a zero range accepts every message for this UI thread.
            while self._user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, _PM_REMOVE
            ):
                if message.message == _WM_QUIT:
                    stop_requested.set()
                    return

                # TranslateMessage creates character messages from keyboard
                # input when relevant; it is harmless for paint-only traffic.
                self._user32.TranslateMessage(ctypes.byref(message))

                # DispatchMessageW invokes _window_proc for this window.
                self._user32.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= deadline:
                return

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
        if self._class_registered:
            return

        # WNDCLASSEXW describes a reusable window *type*, not an individual
        # window. CreateWindowExW later instantiates this description.
        window_class = _WNDCLASSEXW()

        # Windows uses cbSize to determine which version of the structure it
        # received and which trailing fields are safe to read.
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)

        # Repaint the full label after either dimension changes.
        window_class.style = _CS_HREDRAW | _CS_VREDRAW

        # Store the live ctypes callback Windows will invoke for messages.
        window_class.lpfnWndProc = self._window_proc_callback

        # GetModuleHandleW(None) returns the module of this running executable.
        module_handle = self._kernel32.GetModuleHandleW(None)
        window_class.hInstance = module_handle

        # CreateSolidBrush produces the black erasing brush. After layered
        # transparency is enabled, this same black becomes transparent.
        background_brush = self._gdi32.CreateSolidBrush(_TRANSPARENT_COLORKEY)
        window_class.hbrBackground = background_brush

        # This exact name links registration to the later CreateWindowExW call.
        window_class.lpszClassName = _CLASS_NAME

        # RegisterClassExW copies this description into Windows. It returns a
        # zero atom on failure, in which case LastError explains why.
        if not self._user32.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_registered = True

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Draw current text when requested and let Windows handle other messages."""
        if message == _WM_PAINT:
            # PAINTSTRUCT receives bookkeeping that must be passed back to
            # EndPaint after drawing finishes.
            paint = _PAINTSTRUCT()

            # BeginPaint validates the dirty region and supplies a device
            # context—the native drawing surface used by GDI.
            device_context = self._user32.BeginPaint(hwnd, ctypes.byref(paint))

            # GetClientRect returns the drawable interior using coordinates
            # relative to the label's own top-left corner.
            client_rect = wintypes.RECT()
            self._user32.GetClientRect(hwnd, ctypes.byref(client_rect))

            # Draw only centered text. The black background is removed later by
            # color-key transparency, allowing the real taskbar to show through.
            # SetBkMode prevents GDI from drawing an opaque box behind glyphs.
            self._gdi32.SetBkMode(device_context, _TRANSPARENT)

            # SetTextColor chooses the near-white taskbar foreground color.
            self._gdi32.SetTextColor(device_context, _TASKBAR_FOREGROUND)

            # GetStockObject obtains Windows' shared default UI font; SelectObject
            # installs it into this drawing context for the next text operation.
            default_font = self._gdi32.GetStockObject(_DEFAULT_GUI_FONT)
            self._gdi32.SelectObject(
                device_context,
                default_font,
            )

            # DrawTextW lays out the complete Python string (-1 means
            # null-terminated) inside client_rect using the centering flags.
            self._user32.DrawTextW(
                device_context,
                self._window_text,
                -1,
                ctypes.byref(client_rect),
                _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
            )

            # Every successful BeginPaint must be paired with EndPaint so
            # Windows clears the dirty region and releases the drawing context.
            self._user32.EndPaint(hwnd, ctypes.byref(paint))
            return 0

        # DefWindowProcW provides Windows' standard behavior for lifecycle,
        # sizing, cursor, and every other message ClaudeMonitor does not handle.
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _configure_functions(self) -> None:
        """Declare each DLL function's argument and return types for 64-bit safety.

        ``ctypes`` otherwise guesses that values are ordinary C integers, which
        can truncate 64-bit window handles. This method is verbose but contains
        no application behavior; it is the type boundary between Python and
        Windows.
        """
        # Locate taskbar windows and read their geometry.
        # FindWindowW: locate a top-level window by class name/title.
        self._user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        self._user32.FindWindowW.restype = wintypes.HWND

        # FindWindowExW: locate a child window inside a known parent.
        self._user32.FindWindowExW.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        )
        self._user32.FindWindowExW.restype = wintypes.HWND

        # GetWindowRect: copy a window's screen bounds into a RECT pointer.
        self._user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self._user32.GetWindowRect.restype = wintypes.BOOL

        # Create, move, show, parent, and enumerate windows.
        # CreateWindowExW: instantiate the registered class and return its HWND.
        self._user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        self._user32.CreateWindowExW.restype = wintypes.HWND

        # SetWindowPos: move/resize/reorder a window according to flag bits.
        self._user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        self._user32.SetWindowPos.restype = wintypes.BOOL

        # ShowWindow: change visibility using a SW_* command integer.
        self._user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.ShowWindow.restype = wintypes.BOOL

        # SetParent: attach the label to Explorer and return its previous parent.
        self._user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
        self._user32.SetParent.restype = wintypes.HWND

        # GetWindow: traverse parent/child and sibling relationships.
        self._user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self._user32.GetWindow.restype = wintypes.HWND

        # IsWindowVisible: report whether a sibling participates in display.
        self._user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self._user32.IsWindowVisible.restype = wintypes.BOOL

        # IsWindow: validate that an HWND still identifies a live window.
        self._user32.IsWindow.argtypes = (wintypes.HWND,)
        self._user32.IsWindow.restype = wintypes.BOOL

        # DestroyWindow: release a window owned by the current UI thread.
        self._user32.DestroyWindow.argtypes = (wintypes.HWND,)
        self._user32.DestroyWindow.restype = wintypes.BOOL

        # Change window styles and configure transparent backgrounds.
        # SetWindowLongPtrW: replace a pointer-sized style field.
        self._user32.SetWindowLongPtrW.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        )
        self._user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

        # GetWindowLongPtrW: read a pointer-sized style field.
        self._user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

        # SetLayeredWindowAttributes: apply color-key transparency.
        self._user32.SetLayeredWindowAttributes.argtypes = (
            wintypes.HWND,
            wintypes.COLORREF,
            wintypes.BYTE,
            wintypes.DWORD,
        )
        self._user32.SetLayeredWindowAttributes.restype = wintypes.BOOL

        # Change text and dispatch Windows' message queue.
        # SetWindowTextW: update the native Unicode title string.
        self._user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self._user32.SetWindowTextW.restype = wintypes.BOOL

        # InvalidateRect: mark part or all of a window as needing repaint.
        self._user32.InvalidateRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
            wintypes.BOOL,
        )
        self._user32.InvalidateRect.restype = wintypes.BOOL

        # PeekMessageW: non-blockingly remove the next queued thread message.
        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL

        # TranslateMessage: derive character messages from raw keyboard input.
        self._user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.TranslateMessage.restype = wintypes.BOOL

        # DispatchMessageW: call the target window's registered procedure.
        self._user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t

        # Register the custom class and paint its contents.
        # RegisterClassExW: register the class description for this process.
        self._user32.RegisterClassExW.argtypes = (ctypes.POINTER(_WNDCLASSEXW),)
        self._user32.RegisterClassExW.restype = wintypes.ATOM

        # DefWindowProcW: default handling for messages our callback ignores.
        self._user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.DefWindowProcW.restype = ctypes.c_ssize_t

        # BeginPaint: obtain the drawing context for a WM_PAINT operation.
        self._user32.BeginPaint.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_PAINTSTRUCT),
        )
        self._user32.BeginPaint.restype = wintypes.HDC

        # EndPaint: finish drawing and validate the dirty region.
        self._user32.EndPaint.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_PAINTSTRUCT),
        )
        self._user32.EndPaint.restype = wintypes.BOOL

        # GetClientRect: read drawable bounds relative to the window itself.
        self._user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self._user32.GetClientRect.restype = wintypes.BOOL

        # DrawTextW: render a Unicode string inside a rectangle.
        self._user32.DrawTextW.argtypes = (
            wintypes.HDC,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.RECT),
            wintypes.UINT,
        )
        self._user32.DrawTextW.restype = ctypes.c_int

        # Obtain this process's module handle and configure GDI text drawing.
        # GetModuleHandleW: identify the current executable module when passed None.
        self._kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        # CreateSolidBrush: allocate a brush that paints one COLORREF value.
        self._gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
        self._gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

        # SetBkMode: choose transparent or opaque text backgrounds.
        self._gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
        self._gdi32.SetBkMode.restype = ctypes.c_int

        # SetTextColor: choose the foreground color for later text drawing.
        self._gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.COLORREF)
        self._gdi32.SetTextColor.restype = wintypes.COLORREF

        # GetStockObject: retrieve a Windows-owned standard font or brush.
        self._gdi32.GetStockObject.argtypes = (ctypes.c_int,)
        self._gdi32.GetStockObject.restype = wintypes.HGDIOBJ

        # SelectObject: install the chosen font into a drawing context.
        self._gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        self._gdi32.SelectObject.restype = wintypes.HGDIOBJ
