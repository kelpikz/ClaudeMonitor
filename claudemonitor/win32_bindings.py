"""Windows API vocabulary used by the taskbar usage label.

Everything here is a fact about Windows rather than a fact about ClaudeMonitor:
numeric constants, the C structure layouts ``ctypes`` needs, and the table of
function signatures that keeps 64-bit handles from being truncated. Keeping it
separate leaves ``win32_taskbar_window`` free to describe only *behavior*.
"""

from __future__ import annotations

import ctypes
import winreg
from ctypes import wintypes


# Basic window behavior: begin as a standalone popup, optionally visible, then
# convert to a child after Explorer accepts it into the taskbar.
WS_POPUP = 0x80000000  # Create a top-level window before taskbar attachment.
WS_CHILD = 0x40000000  # Make coordinates and lifetime belong to the taskbar.
WS_EX_TOOLWINDOW = 0x00000080  # Keep the helper out of Alt+Tab.
WS_EX_NOACTIVATE = 0x08000000  # Never steal keyboard focus.
WS_EX_TOPMOST = 0x00000008  # Keep a tooltip above the taskbar it describes.
GWL_STYLE = -16  # Select the ordinary style field in Get/SetWindowLongPtr.
GWL_EXSTYLE = -20  # Select the extended-style field in Get/SetWindowLongPtr.

# Transparency: pixels painted black become holes through which the taskbar's
# own acrylic background remains visible.
WS_EX_LAYERED = 0x00080000  # Allow per-pixel transparency configuration.
LWA_COLORKEY = 0x00000001  # Treat one chosen color as fully transparent.
TRANSPARENT_COLORKEY = 0x00000000  # Black pixels will reveal the taskbar.

# Repositioning flags. Moving must not activate or accidentally show a window;
# visibility is controlled separately through ShowWindow.
SWP_NOSIZE = 0x0001  # Preserve width and height during a style-only update.
SWP_NOMOVE = 0x0002  # Preserve x and y during a style-only update.
SWP_NOZORDER = 0x0004  # Preserve stacking order relative to other windows.
SWP_NOACTIVATE = 0x0010  # Do not move keyboard focus to this window.
SWP_FRAMECHANGED = 0x0020  # Recalculate the frame after changing styles.
HWND_TOPMOST = -1  # Place the fallback popup above other normal windows.

# ShowWindow commands: reveal without stealing focus, or hide entirely.
SW_HIDE = 0
SW_SHOWNOACTIVATE = 8

# Windows sends messages to request painting, shutdown, and theme updates.
# PeekMessage removes each message from the queue before it is dispatched.
WM_PAINT = 0x000F  # Windows is asking the window to redraw itself.
WM_QUIT = 0x0012  # The thread's message loop should end.
WM_SETTINGCHANGE = 0x001A  # A system setting, including light/dark mode, changed.
WM_THEMECHANGED = 0x031A  # The visual style changed.
PM_REMOVE = 0x0001  # Remove messages as PeekMessage reads them.

# Standard Windows tooltip-control messages and tracking behavior.
TOOLTIPS_CLASS = "tooltips_class32"
TTS_ALWAYSTIP = 0x0001
TTS_NOPREFIX = 0x0002
TTF_IDISHWND = 0x0001
TTF_TRACK = 0x0020
WM_USER = 0x0400
TTM_SETMAXTIPWIDTH = WM_USER + 24
TTM_SETTIPBKCOLOR = WM_USER + 19
TTM_SETTIPTEXTCOLOR = WM_USER + 20
TTM_TRACKACTIVATE = WM_USER + 17
TTM_TRACKPOSITION = WM_USER + 18
TTM_UPDATE = WM_USER + 29
TTM_ADDTOOLW = WM_USER + 50
TTM_UPDATETIPTEXTW = WM_USER + 57
ICC_WIN95_CLASSES = 0x000000FF  # Includes the standard tooltip control class.

# Text drawing options: center one line both horizontally and vertically and
# draw without a background rectangle.
DT_CENTER = 0x00000001  # Center text horizontally.
DT_VCENTER = 0x00000004  # Center text vertically.
DT_SINGLELINE = 0x00000020  # Keep the usage summary on one line.
TRANSPARENT_BACKGROUND = 1  # Do not let GDI paint a background behind glyphs.
DEFAULT_GUI_FONT = 17  # Windows stock font identifier for standard UI text.

# Taskbar text must contrast with the theme the user actually runs; near-white
# glyphs are invisible on a Windows 11 light-mode taskbar.
DARK_THEME_FOREGROUND = 0x00F5F5F5  # Near-white COLORREF in BGR byte order.
LIGHT_THEME_FOREGROUND = 0x001A1A1A  # Near-black COLORREF in BGR byte order.
_THEME_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_THEME_REGISTRY_VALUE = "SystemUsesLightTheme"

# Window-class and sibling-enumeration values used to register our label and
# walk the taskbar's direct child windows.
CS_HREDRAW = 0x0002  # Repaint after horizontal resizing.
CS_VREDRAW = 0x0001  # Repaint after vertical resizing.
CLASS_NAME = "ClaudeMonitorTaskbarWindow"  # Process-local window type name.
GW_HWNDNEXT = 2  # Continue to the next sibling window.
GW_CHILD = 5  # Start at a parent's first child window.
ERROR_CLASS_ALREADY_EXISTS = 1410  # A second instance registered the class first.

# Font metrics.
SPI_GETNONCLIENTMETRICS = 0x0029  # Ask Windows for the current UI font metrics.

# Blitting the small Claude glyph beside the usage text as an uncompressed,
# top-down 24bpp bitmap.
BI_RGB = 0  # Uncompressed pixel data; no compression bookkeeping needed.
DIB_RGB_COLORS = 0  # The DIB's color table holds literal RGB values (unused at 24bpp).


def _int_resource(identifier: int) -> wintypes.LPCWSTR:
    """Pack a numeric resource id into the string pointer Win32 expects.

    This is Windows' MAKEINTRESOURCE macro. Functions such as LoadCursorW accept
    either a resource *name* or a small integer stuffed into the same pointer
    argument, and ctypes will not perform that reinterpretation itself: handing
    the bare integer to an LPCWSTR parameter raises ArgumentError instead.
    """
    return ctypes.cast(ctypes.c_void_p(identifier), wintypes.LPCWSTR)


# Standard arrow cursor shared by ordinary windows, as the packed resource
# pointer LoadCursorW takes rather than the raw 32512 the documentation quotes.
IDC_ARROW = _int_resource(32512)


# A window procedure is the callback Windows invokes whenever our label needs
# to paint or receives another operating-system message.
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT: pointer-sized value returned to Windows.
    wintypes.HWND,  # HWND: window receiving the message.
    wintypes.UINT,  # UINT: numeric message identifier such as WM_PAINT.
    wintypes.WPARAM,  # WPARAM: message-specific pointer-sized input.
    wintypes.LPARAM,  # LPARAM: second message-specific pointer-sized input.
)


class WNDCLASSEXW(ctypes.Structure):
    """Python layout of the Win32 structure used to register a window type."""

    _fields_ = [
        ("cbSize", wintypes.UINT),  # Byte size, used for versioning the structure.
        ("style", wintypes.UINT),  # Redraw behavior shared by every window.
        ("lpfnWndProc", WNDPROC),  # Callback that handles Windows messages.
        ("cbClsExtra", ctypes.c_int),  # Extra class bytes; ClaudeMonitor needs none.
        ("cbWndExtra", ctypes.c_int),  # Extra per-window bytes; also unused.
        ("hInstance", wintypes.HINSTANCE),  # Module that owns this window class.
        ("hIcon", wintypes.HICON),  # Large icon; omitted for the taskbar label.
        ("hCursor", wintypes.HANDLE),  # Cursor shown while hovering the label.
        ("hbrBackground", wintypes.HBRUSH),  # Brush used to erase the background.
        ("lpszMenuName", wintypes.LPCWSTR),  # Native menu resource; none is attached.
        ("lpszClassName", wintypes.LPCWSTR),  # Name passed to CreateWindowExW.
        ("hIconSm", wintypes.HICON),  # Small icon; omitted for the taskbar label.
    ]


class PAINTSTRUCT(ctypes.Structure):
    """Python layout of the drawing information Windows supplies while painting."""

    _fields_ = [
        ("hdc", wintypes.HDC),  # Drawing context prepared by BeginPaint.
        ("fErase", wintypes.BOOL),  # Whether Windows erased the background.
        ("rcPaint", wintypes.RECT),  # Region that needs repainting.
        ("fRestore", wintypes.BOOL),  # Reserved Windows bookkeeping value.
        ("fIncUpdate", wintypes.BOOL),  # Reserved Windows bookkeeping value.
        ("rgbReserved", ctypes.c_byte * 32),  # Private state owned by Windows.
    ]


class TOOLINFOW(ctypes.Structure):
    """Python layout describing one window tracked by a tooltip control."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("uId", ctypes.c_size_t),
        ("rect", wintypes.RECT),
        ("hinst", wintypes.HINSTANCE),
        ("lpszText", wintypes.LPWSTR),
        ("lParam", wintypes.LPARAM),
        ("lpReserved", wintypes.LPVOID),
    ]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    """Python layout selecting which common-control classes Windows registers."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwICC", wintypes.DWORD),
    ]


class LOGFONTW(ctypes.Structure):
    """Python layout of a Windows font description."""

    _fields_ = [
        ("lfHeight", wintypes.LONG),  # Character height; negative means point-based.
        ("lfWidth", wintypes.LONG),  # Average character width; zero means automatic.
        ("lfEscapement", wintypes.LONG),  # Text angle in tenths of a degree.
        ("lfOrientation", wintypes.LONG),  # Glyph angle in tenths of a degree.
        ("lfWeight", wintypes.LONG),  # Boldness from 0 to 900.
        ("lfItalic", wintypes.BYTE),
        ("lfUnderline", wintypes.BYTE),
        ("lfStrikeOut", wintypes.BYTE),
        ("lfCharSet", wintypes.BYTE),  # Character set the face is requested in.
        ("lfOutPrecision", wintypes.BYTE),  # How closely the match must be honored.
        ("lfClipPrecision", wintypes.BYTE),  # How glyphs outside the region are clipped.
        ("lfQuality", wintypes.BYTE),  # Anti-aliasing preference.
        ("lfPitchAndFamily", wintypes.BYTE),  # Pitch plus stylistic family.
        ("lfFaceName", wintypes.WCHAR * 32),  # Typeface name such as "Segoe UI".
    ]


class SIZE(ctypes.Structure):
    """Python layout of a GDI text measurement, in logical pixels."""

    _fields_ = [
        ("cx", ctypes.c_long),
        ("cy", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    """Python layout of the header describing an uncompressed DIB's pixel data."""

    _fields_ = [
        ("biSize", wintypes.DWORD),  # Byte size, used for versioning the structure.
        ("biWidth", ctypes.c_long),
        # Negative height marks a top-down DIB, so row 0 is the top row PIL
        # already produced rather than the bottom-up order DIBs default to.
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),  # Always 1 for device-independent bitmaps.
        ("biBitCount", wintypes.WORD),  # Bits per pixel; 24 needs no color table.
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),  # May be 0 for uncompressed BI_RGB data.
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    """Python layout of a DIB description; ``bmiColors`` is unused at 24bpp
    but must still be present so the structure's size matches Windows'."""

    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class NONCLIENTMETRICSW(ctypes.Structure):
    """Python layout of the system's window-decoration and UI font metrics."""

    _fields_ = [
        ("cbSize", wintypes.UINT),  # Byte size, used for versioning the structure.
        ("iBorderWidth", ctypes.c_int),
        ("iScrollWidth", ctypes.c_int),
        ("iScrollHeight", ctypes.c_int),
        ("iCaptionWidth", ctypes.c_int),
        ("iCaptionHeight", ctypes.c_int),
        ("lfCaptionFont", LOGFONTW),
        ("iSmCaptionWidth", ctypes.c_int),
        ("iSmCaptionHeight", ctypes.c_int),
        ("lfSmCaptionFont", LOGFONTW),
        ("iMenuWidth", ctypes.c_int),
        ("iMenuHeight", ctypes.c_int),
        ("lfMenuFont", LOGFONTW),
        ("lfStatusFont", LOGFONTW),
        ("lfMessageFont", LOGFONTW),  # The font Windows uses for ordinary UI text.
    ]


# Each entry maps a DLL function to its (argument types, return type). Declaring
# these before any call stops ctypes from assuming C ints and truncating 64-bit
# window handles, device contexts, and message parameters.
USER32_SIGNATURES: dict[str, tuple[tuple, object]] = {
    # Locate taskbar windows and read their geometry.
    "FindWindowW": ((wintypes.LPCWSTR, wintypes.LPCWSTR), wintypes.HWND),
    "FindWindowExW": (
        (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR),
        wintypes.HWND,
    ),
    "GetWindowRect": ((wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
    "GetClientRect": ((wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
    "GetCursorPos": ((ctypes.POINTER(wintypes.POINT),), wintypes.BOOL),
    # Create, move, show, parent, and enumerate windows.
    "CreateWindowExW": (
        (
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
        ),
        wintypes.HWND,
    ),
    "SetWindowPos": (
        (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ),
        wintypes.BOOL,
    ),
    "ShowWindow": ((wintypes.HWND, ctypes.c_int), wintypes.BOOL),
    "SetParent": ((wintypes.HWND, wintypes.HWND), wintypes.HWND),
    "GetWindow": ((wintypes.HWND, wintypes.UINT), wintypes.HWND),
    "IsWindowVisible": ((wintypes.HWND,), wintypes.BOOL),
    "IsWindow": ((wintypes.HWND,), wintypes.BOOL),
    "DestroyWindow": ((wintypes.HWND,), wintypes.BOOL),
    # Change window styles and configure transparent backgrounds.
    "SetWindowLongPtrW": (
        (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t),
        ctypes.c_ssize_t,
    ),
    "GetWindowLongPtrW": ((wintypes.HWND, ctypes.c_int), ctypes.c_ssize_t),
    "SetLayeredWindowAttributes": (
        (wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD),
        wintypes.BOOL,
    ),
    # Change text and dispatch Windows' message queue.
    "SetWindowTextW": ((wintypes.HWND, wintypes.LPCWSTR), wintypes.BOOL),
    "InvalidateRect": (
        (wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL),
        wintypes.BOOL,
    ),
    "PeekMessageW": (
        (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ),
        wintypes.BOOL,
    ),
    "TranslateMessage": ((ctypes.POINTER(wintypes.MSG),), wintypes.BOOL),
    "DispatchMessageW": ((ctypes.POINTER(wintypes.MSG),), ctypes.c_ssize_t),
    "SendMessageW": (
        (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        ctypes.c_ssize_t,
    ),
    # Register the custom class and paint its contents.
    "RegisterClassExW": ((ctypes.POINTER(WNDCLASSEXW),), wintypes.ATOM),
    "LoadCursorW": ((wintypes.HINSTANCE, wintypes.LPCWSTR), wintypes.HANDLE),
    "DefWindowProcW": (
        (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        ctypes.c_ssize_t,
    ),
    "BeginPaint": ((wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)), wintypes.HDC),
    "EndPaint": ((wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)), wintypes.BOOL),
    "DrawTextW": (
        (
            wintypes.HDC,
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.RECT),
            wintypes.UINT,
        ),
        ctypes.c_int,
    ),
    # Read the system's UI font metrics.
    "SystemParametersInfoW": (
        (wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT),
        wintypes.BOOL,
    ),
}

GDI32_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "CreateSolidBrush": ((wintypes.COLORREF,), wintypes.HBRUSH),
    "CreateFontIndirectW": ((ctypes.POINTER(LOGFONTW),), wintypes.HGDIOBJ),
    "SetBkMode": ((wintypes.HDC, ctypes.c_int), ctypes.c_int),
    "SetTextColor": ((wintypes.HDC, wintypes.COLORREF), wintypes.COLORREF),
    "GetStockObject": ((ctypes.c_int,), wintypes.HGDIOBJ),
    "SelectObject": ((wintypes.HDC, wintypes.HGDIOBJ), wintypes.HGDIOBJ),
    # Measuring text width to size the label to its actual content.
    "CreateCompatibleDC": ((wintypes.HDC,), wintypes.HDC),
    "DeleteDC": ((wintypes.HDC,), wintypes.BOOL),
    "GetTextExtentPoint32W": (
        (wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE)),
        wintypes.BOOL,
    ),
    "SetDIBitsToDevice": (
        (
            wintypes.HDC,
            ctypes.c_int,  # xDest
            ctypes.c_int,  # yDest
            wintypes.DWORD,  # dwWidth
            wintypes.DWORD,  # dwHeight
            ctypes.c_int,  # xSrc
            ctypes.c_int,  # ySrc
            wintypes.UINT,  # uStartScan
            wintypes.UINT,  # cScanLines
            ctypes.c_void_p,  # lpvBits
            ctypes.POINTER(BITMAPINFO),  # lpbmi
            wintypes.UINT,  # fuColorUse
        ),
        ctypes.c_int,
    ),
}

COMCTL32_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "InitCommonControlsEx": (
        (ctypes.POINTER(INITCOMMONCONTROLSEX),),
        wintypes.BOOL,
    ),
}

UXTHEME_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "SetWindowTheme": (
        (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR),
        ctypes.c_long,  # HRESULT
    ),
}

KERNEL32_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "GetModuleHandleW": ((wintypes.LPCWSTR,), wintypes.HMODULE),
}


def apply_signatures(
    dll: object,
    signatures: dict[str, tuple[tuple, object]],
) -> list[str]:
    """Declare argument and return types for every function in a signature table.

    Returns the names Windows does not export on this build. Some entries only
    exist on newer releases, and one absent export must not stop the rest of the
    table — or the application — from loading.
    """
    missing: list[str] = []
    for name, (argument_types, return_type) in signatures.items():
        try:
            function = getattr(dll, name)
        except AttributeError:
            missing.append(name)
            continue
        function.argtypes = argument_types
        function.restype = return_type
    return missing


def foreground_color_for_theme(*, uses_light_theme: bool) -> int:
    """Pick taskbar text color that stays readable against the active theme."""
    return LIGHT_THEME_FOREGROUND if uses_light_theme else DARK_THEME_FOREGROUND


def system_uses_light_theme() -> bool:
    """Report whether Windows is currently drawing its shell in light mode."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_REGISTRY_KEY) as key:
            value, _value_type = winreg.QueryValueEx(key, _THEME_REGISTRY_VALUE)
    except OSError:
        # The value is absent on older builds, where dark taskbars are the norm.
        return False
    return bool(value)
