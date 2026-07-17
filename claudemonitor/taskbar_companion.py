from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Final, Protocol


log = logging.getLogger(__name__)

HELLO_WORLD_TEXT: Final = "Claude: loading..."
_COMPANION_WIDTH: Final = 180
# How often the companion re-checks taskbar geometry (and, in fallback mode,
# re-asserts its topmost position against the taskbar).
_REPOSITION_INTERVAL_SECONDS: Final = 1.0

_WS_POPUP: Final = 0x80000000
_WS_VISIBLE: Final = 0x10000000
_WS_EX_TOOLWINDOW: Final = 0x00000080
_WS_EX_NOACTIVATE: Final = 0x08000000
_WS_EX_LAYERED: Final = 0x00080000
_LWA_COLORKEY: Final = 0x00000001
_CS_HREDRAW: Final = 0x0002
_CS_VREDRAW: Final = 0x0001
_SWP_NOACTIVATE: Final = 0x0010
_SWP_SHOWWINDOW: Final = 0x0040
_WM_PAINT: Final = 0x000F
_WM_QUIT: Final = 0x0012
_PM_REMOVE: Final = 0x0001
_DT_CENTER: Final = 0x00000001
_DT_VCENTER: Final = 0x00000004
_DT_SINGLELINE: Final = 0x00000020
_TRANSPARENT: Final = 1
_DEFAULT_GUI_FONT: Final = 17
# Pixels painted in this color become fully transparent, so the taskbar's own
# acrylic background shows through and only the text stays visible.
_TRANSPARENT_COLORKEY: Final = 0x00000000
_TASKBAR_FOREGROUND: Final = 0x00F5F5F5
_CLASS_NAME: Final = "ClaudeMonitorTaskbarWindow"
_GWL_EXSTYLE: Final = -20
_HWND_TOPMOST: Final = -1
_GW_HWNDNEXT: Final = 2
_GW_CHILD: Final = 5


@dataclass(frozen=True)
class Rect:
    """Represent Win32 rectangle coordinates."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def leftmost_abutting_edge(
    anchor_left: int,
    children: list[Rect],
    tolerance: int = 8,
) -> int:
    """Chain left across sibling windows (other taskbar plugins) abutting the anchor."""
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
    """Place a child window directly before the Windows notification area."""
    right = notification.left - taskbar.left
    return Rect(
        left=max(0, right - width),
        top=0,
        right=right,
        bottom=taskbar.height,
    )


class NativeWindow(Protocol):
    """Describe the native operations used by the companion controller."""

    def find_taskbar(self) -> int: ...
    def find_notification_area(self, taskbar: int) -> int: ...
    def get_rect(self, handle: int) -> Rect: ...
    def create_window(self, *, parent: int, style: int, ex_style: int, text: str) -> int: ...
    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool: ...
    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]: ...
    def set_colorkey_transparency(self, handle: int) -> None: ...
    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None: ...
    def set_text(self, handle: int, text: str) -> None: ...
    def set_visible(self, handle: int, visible: bool) -> None: ...
    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None: ...
    def close_window(self, handle: int) -> None: ...


class TaskbarCompanion:
    """Own a native Win32 child window embedded in Explorer's taskbar."""

    def __init__(
        self,
        *,
        native: NativeWindow | None = None,
        initial_visible: bool = True,
    ) -> None:
        self._native = native or Win32TaskbarWindow()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._display_lock = threading.Lock()
        self._text = HELLO_WORLD_TEXT
        self._visible = initial_visible

    @property
    def visible(self) -> bool:
        """Return the user-selected visibility state."""
        with self._display_lock:
            return self._visible

    def update(self, text: str) -> None:
        """Request new text for the native window."""
        with self._display_lock:
            self._text = text

    def set_visible(self, visible: bool) -> None:
        """Request that the native window be shown or hidden."""
        with self._display_lock:
            self._visible = visible

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
        """Request shutdown and briefly join the native UI thread."""
        self._stop_requested.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        """Create, embed, keep positioned, and destroy one native taskbar window."""
        window_handle = 0
        try:
            with self._display_lock:
                rendered_text = self._text
                rendered_visible = self._visible
            taskbar = self._native.find_taskbar()
            window_handle = self._native.create_window(
                parent=0,
                style=_WS_POPUP | (_WS_VISIBLE if rendered_visible else 0),
                ex_style=_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
                text=rendered_text,
            )
            attached = self._native.attach_to_taskbar(window_handle, taskbar)
            if attached:
                self._native.set_colorkey_transparency(window_handle)
            else:
                log.warning("taskbar parenting failed; using topmost screen popup")
            position = self._compute_position(window_handle, attached=attached)
            self._native.move_window(window_handle, position, topmost=not attached)
            log.info(
                "taskbar companion window=%s initialized attached=%s at %s",
                window_handle,
                attached,
                position,
            )
            while not self._stop_requested.is_set():
                self._native.pump_messages(
                    self._stop_requested,
                    _REPOSITION_INTERVAL_SECONDS,
                )
                with self._display_lock:
                    requested_text = self._text
                    requested_visible = self._visible
                if requested_text != rendered_text:
                    self._native.set_text(window_handle, requested_text)
                    rendered_text = requested_text
                if requested_visible != rendered_visible:
                    self._native.set_visible(window_handle, requested_visible)
                    rendered_visible = requested_visible
                new_position = self._compute_position(window_handle, attached=attached)
                if new_position != position:
                    log.info("taskbar companion repositioned to %s", new_position)
                    position = new_position
                    self._native.move_window(window_handle, position, topmost=not attached)
                elif not attached:
                    # The fallback popup competes with the taskbar for topmost,
                    # so it must re-assert its place even when nothing moved.
                    self._native.move_window(window_handle, position, topmost=True)
        except Exception:
            log.exception("native taskbar companion failed")
        finally:
            if window_handle:
                self._native.close_window(window_handle)

    def _compute_position(self, window_handle: int, *, attached: bool) -> Rect:
        """Locate the free slot before the tray and any other embedded plugins."""
        taskbar = self._native.find_taskbar()
        notification = self._native.find_notification_area(taskbar)
        taskbar_rect = self._native.get_rect(taskbar)
        notification_rect = self._native.get_rect(notification)
        siblings = self._native.list_sibling_rects(taskbar, window_handle)
        anchored_notification = Rect(
            left=leftmost_abutting_edge(notification_rect.left, siblings),
            top=notification_rect.top,
            right=notification_rect.right,
            bottom=notification_rect.bottom,
        )
        position = taskbar_child_rect(taskbar_rect, anchored_notification, _COMPANION_WIDTH)
        if attached:
            return position
        return Rect(
            left=position.left + taskbar_rect.left,
            top=position.top + taskbar_rect.top,
            right=position.right + taskbar_rect.left,
            bottom=position.bottom + taskbar_rect.top,
        )


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class Win32TaskbarWindow:
    """Implement a small native window using user32 and GDI."""

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()
        self._window_proc_callback = _WNDPROC(self._window_proc)
        self._class_registered = False
        self._window_text = ""

    def find_taskbar(self) -> int:
        """Return Explorer's primary taskbar handle."""
        handle = self._user32.FindWindowW("Shell_TrayWnd", None)
        if not handle:
            raise RuntimeError("Windows taskbar was not found")
        return handle

    def find_notification_area(self, taskbar: int) -> int:
        """Return the notification area hosted by the primary taskbar."""
        handle = self._user32.FindWindowExW(taskbar, None, "TrayNotifyWnd", None)
        if not handle:
            raise RuntimeError("Windows notification area was not found")
        return handle

    def get_rect(self, handle: int) -> Rect:
        """Read a window rectangle in screen coordinates."""
        raw = wintypes.RECT()
        if not self._user32.GetWindowRect(handle, ctypes.byref(raw)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Rect(raw.left, raw.top, raw.right, raw.bottom)

    def create_window(self, *, parent: int, style: int, ex_style: int, text: str) -> int:
        """Register and create the native taskbar child window."""
        self._register_class()
        self._window_text = text
        handle = self._user32.CreateWindowExW(
            ex_style,
            _CLASS_NAME,
            text,
            style,
            0,
            0,
            1,
            1,
            parent,
            None,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def attach_to_taskbar(self, handle: int, taskbar: int) -> bool:
        """Embed the popup into Explorer's taskbar; success mirrors TrafficMonitor's check."""
        return bool(self._user32.SetParent(handle, taskbar))

    def list_sibling_rects(self, taskbar: int, exclude_handle: int) -> list[Rect]:
        """Collect visible taskbar children that may already occupy plugin space."""
        rects: list[Rect] = []
        child = self._user32.GetWindow(taskbar, _GW_CHILD)
        while child:
            if child != exclude_handle and self._user32.IsWindowVisible(child):
                rect = self.get_rect(child)
                if rect.width > 0 and rect.height > 0:
                    rects.append(rect)
            child = self._user32.GetWindow(child, _GW_HWNDNEXT)
        return rects

    def set_colorkey_transparency(self, handle: int) -> None:
        """Make the window background transparent so the taskbar shows through."""
        ex_style = self._user32.GetWindowLongPtrW(handle, _GWL_EXSTYLE)
        self._user32.SetWindowLongPtrW(handle, _GWL_EXSTYLE, ex_style | _WS_EX_LAYERED)
        if not self._user32.SetLayeredWindowAttributes(
            handle,
            _TRANSPARENT_COLORKEY,
            0,
            _LWA_COLORKEY,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def move_window(self, handle: int, rect: Rect, *, topmost: bool) -> None:
        """Place the window; parent-relative when embedded, screen coordinates otherwise."""
        if not self._user32.SetWindowPos(
            handle,
            _HWND_TOPMOST if topmost else None,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def set_text(self, handle: int, text: str) -> None:
        """Replace the painted label and request a redraw."""
        self._window_text = text
        if not self._user32.SetWindowTextW(handle, text):
            raise ctypes.WinError(ctypes.get_last_error())
        self._user32.InvalidateRect(handle, None, True)

    def set_visible(self, handle: int, visible: bool) -> None:
        """Show without activation or hide the taskbar window."""
        self._user32.ShowWindow(handle, 8 if visible else 0)

    def pump_messages(self, stop_requested: threading.Event, duration_seconds: float) -> None:
        """Dispatch messages until the duration elapses or shutdown is requested."""
        deadline = time.monotonic() + duration_seconds
        message = wintypes.MSG()
        while not stop_requested.wait(0.05):
            while self._user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, _PM_REMOVE
            ):
                if message.message == _WM_QUIT:
                    stop_requested.set()
                    return
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= deadline:
                return

    def close_window(self, handle: int) -> None:
        """Destroy the window on the same thread that created it."""
        if self._user32.IsWindow(handle):
            self._user32.DestroyWindow(handle)

    def _register_class(self) -> None:
        """Register the process-local native window class once."""
        if self._class_registered:
            return
        window_class = _WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        window_class.style = _CS_HREDRAW | _CS_VREDRAW
        window_class.lpfnWndProc = self._window_proc_callback
        window_class.hInstance = self._kernel32.GetModuleHandleW(None)
        window_class.hbrBackground = self._gdi32.CreateSolidBrush(_TRANSPARENT_COLORKEY)
        window_class.lpszClassName = _CLASS_NAME
        if not self._user32.RegisterClassExW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_registered = True

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Paint the label and delegate every other message to Windows."""
        if message == _WM_PAINT:
            paint = _PAINTSTRUCT()
            device_context = self._user32.BeginPaint(hwnd, ctypes.byref(paint))
            client_rect = wintypes.RECT()
            self._user32.GetClientRect(hwnd, ctypes.byref(client_rect))
            self._gdi32.SetBkMode(device_context, _TRANSPARENT)
            self._gdi32.SetTextColor(device_context, _TASKBAR_FOREGROUND)
            self._gdi32.SelectObject(
                device_context,
                self._gdi32.GetStockObject(_DEFAULT_GUI_FONT),
            )
            self._user32.DrawTextW(
                device_context,
                self._window_text,
                -1,
                ctypes.byref(client_rect),
                _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
            )
            self._user32.EndPaint(hwnd, ctypes.byref(paint))
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _configure_functions(self) -> None:
        """Assign pointer-safe ctypes signatures to the Win32 functions used."""
        self._user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        self._user32.FindWindowW.restype = wintypes.HWND
        self._user32.FindWindowExW.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        )
        self._user32.FindWindowExW.restype = wintypes.HWND
        self._user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self._user32.GetWindowRect.restype = wintypes.BOOL
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
        self._user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self._user32.SetWindowTextW.restype = wintypes.BOOL
        self._user32.InvalidateRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
            wintypes.BOOL,
        )
        self._user32.InvalidateRect.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.SetParent.argtypes = (wintypes.HWND, wintypes.HWND)
        self._user32.SetParent.restype = wintypes.HWND
        self._user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self._user32.GetWindow.restype = wintypes.HWND
        self._user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.SetWindowLongPtrW.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_ssize_t,
        )
        self._user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user32.SetLayeredWindowAttributes.argtypes = (
            wintypes.HWND,
            wintypes.COLORREF,
            wintypes.BYTE,
            wintypes.DWORD,
        )
        self._user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        self._user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        self._user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self._user32.IsWindow.argtypes = (wintypes.HWND,)
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.DestroyWindow.argtypes = (wintypes.HWND,)
        self._user32.DestroyWindow.restype = wintypes.BOOL
        self._user32.RegisterClassExW.argtypes = (ctypes.POINTER(_WNDCLASSEXW),)
        self._user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self._user32.RegisterClassExW.restype = wintypes.ATOM
        self._user32.BeginPaint.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_PAINTSTRUCT),
        )
        self._user32.BeginPaint.restype = wintypes.HDC
        self._user32.EndPaint.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(_PAINTSTRUCT),
        )
        self._user32.GetClientRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self._user32.DrawTextW.argtypes = (
            wintypes.HDC,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.RECT),
            wintypes.UINT,
        )
        self._kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self._gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
        self._gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self._gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
        self._gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.COLORREF)
        self._gdi32.GetStockObject.argtypes = (ctypes.c_int,)
        self._gdi32.GetStockObject.restype = wintypes.HGDIOBJ
        self._gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
