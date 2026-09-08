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
GWLP_WNDPROC = -4  # Select the window procedure, which is how a control is subclassed.

# Transparency: the label hands Windows a bitmap carrying an alpha value for
# every pixel, so a half-covered edge is composited against whatever the
# taskbar actually shows there rather than against a colour we had to guess.
WS_EX_LAYERED = 0x00080000  # Allow per-pixel transparency configuration.
ULW_ALPHA = 0x00000002  # Read the source bitmap's own alpha channel.
AC_SRC_OVER = 0x00  # The only blend operation Windows defines.
AC_SRC_ALPHA = 0x01  # The source colours are already multiplied by their alpha.

# Repositioning flags. Moving must not activate or accidentally show a window;
# visibility is controlled separately through ShowWindow.
SWP_NOSIZE = 0x0001  # Preserve width and height during a style-only update.
SWP_NOMOVE = 0x0002  # Preserve x and y during a style-only update.
SWP_NOZORDER = 0x0004  # Preserve stacking order relative to other windows.
SWP_NOACTIVATE = 0x0010  # Do not move keyboard focus to this window.
SWP_FRAMECHANGED = 0x0020  # Recalculate the frame after changing styles.
HWND_TOPMOST = -1  # Place the fallback popup above other normal windows.
HWND_BOTTOM = 1  # Send a window below every sibling it shares a parent with.

# ShowWindow commands: reveal without stealing focus, or hide entirely.
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 8

# An ordinary dialog: a caption bar with a close button, fixed size, and no
# minimise or maximise box. The settings window is the only one of these.
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_VISIBLE = 0x10000000
WS_TABSTOP = 0x00010000
WS_GROUP = 0x00020000
WS_EX_DLGMODALFRAME = 0x00000001
CW_USEDEFAULT = -2147483648  # 0x80000000 as a signed int: "you choose".
WS_CLIPCHILDREN = 0x02000000  # Never paint over a child; the tab page relies on it.
WS_CLIPSIBLINGS = 0x04000000  # Keep a page control out of the tab control's paint.
WS_BORDER = 0x00800000
WS_EX_CONTROLPARENT = 0x00010000  # Let Tab reach the controls inside this window.
IDOK = 1  # The command id Windows itself sends for Enter.
IDCANCEL = 2  # The command id Windows itself sends for Escape.

# The BUTTON class covers both the checkboxes and the push buttons; the style
# bit is the only difference between them.
BUTTON_CLASS = "BUTTON"
BS_AUTOCHECKBOX = 0x00000003  # A checkbox that flips itself when clicked.
BS_PUSHBUTTON = 0x00000000
BS_DEFPUSHBUTTON = 0x00000001
BS_GROUPBOX = 0x00000007  # The captioned box a group of settings sits in.
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BST_UNCHECKED = 0
BST_CHECKED = 1
BN_CLICKED = 0  # The WM_COMMAND notification a button press arrives as.

# The static class prints a label and, with no text, paints the tab page.
STATIC_CLASS = "STATIC"
SS_LEFT = 0x00000000

# The edit class holds a number; ES_NUMBER keeps letters out of it entirely.
EDIT_CLASS = "EDIT"
ES_LEFT = 0x00000000
ES_NUMBER = 0x00002000
ES_AUTOHSCROLL = 0x00000080
EN_CHANGE = 0x0300  # The notification sent after the text has changed.

# The tab strip across the top of the settings dialog.
TAB_CONTROL_CLASS = "SysTabControl32"
TCS_FOCUSNEVER = 0x00008000  # The tabs are reached with Ctrl+Tab, not Tab.
TCM_FIRST = 0x1300
TCM_GETCURSEL = TCM_FIRST + 11
TCM_SETCURSEL = TCM_FIRST + 12
TCM_ADJUSTRECT = TCM_FIRST + 40
TCM_GETITEMCOUNT = TCM_FIRST + 4
TCM_GETITEMRECT = TCM_FIRST + 10
TCM_INSERTITEMW = TCM_FIRST + 62
TCIF_TEXT = 0x0001
TCN_FIRST = -550
TCN_SELCHANGE = TCN_FIRST - 1  # The user moved to another tab.

# The spin arrows beside a number box.
UPDOWN_CLASS = "msctls_updown32"
UDS_ALIGNRIGHT = 0x0004  # Sit against the right edge of the box it serves.
UDS_SETBUDDYINT = 0x0002  # Write the new number into that box.
UDS_ARROWKEYS = 0x0020  # Up and down on the keyboard count as clicks.
UDS_NOTHOUSANDS = 0x0080  # 1000, never 1,000: the box is parsed as an int.
# The spin messages live above WM_USER, which is defined with the tooltips.
UDM_SETBUDDY = 0x0400 + 105  # Name the box the arrows write into.
UDM_SETRANGE32 = 0x0400 + 111  # The lowest and highest the arrows may reach.
UDM_SETPOS32 = 0x0400 + 113  # Where the arrows currently stand.

# Messages a dialog handles: a control was used, the window should go away, and
# the two colour requests that let a dark theme reach the controls.
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
# A themed control paints no background of its own. It asks its parent to
# paint one, through this message, handing over its own device context.
WM_PRINTCLIENT = 0x0318
WM_SETFONT = 0x0030
WM_COMMAND = 0x0111
WM_NOTIFY = 0x004E  # How a common control, such as the tab strip, reports.
WM_CTLCOLOREDIT = 0x0133
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLORBTN = 0x0135

# Screen metrics used to centre the window on the primary display.
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# Dark mode. The title bar is Desktop Window Manager's to paint, and the
# controls take their dark glyphs from the same visual style File Explorer uses.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DARK_MODE_CONTROL_THEME = "DarkMode_Explorer"
DARK_THEME_BACKGROUND = 0x00202020  # Near-black COLORREF in BGR byte order.
LIGHT_THEME_BACKGROUND = 0x00F0F0F0  # The standard dialog grey.
# A tab page is lighter than the dialog around it, and a text box lighter
# still. Both are painted by us rather than by the theme, because the tab
# control has no dark rendering of its own to inherit.
DARK_THEME_PAGE_BACKGROUND = 0x002B2B2B
LIGHT_THEME_PAGE_BACKGROUND = 0x00FFFFFF
DARK_THEME_FIELD_BACKGROUND = 0x003C3C3C
LIGHT_THEME_FIELD_BACKGROUND = 0x00FFFFFF
# The tab strip is drawn by hand in dark mode, so it needs colours of its
# own: a tab that is not the current one, the line around the page, and the
# dimmer text an unselected tab is titled in.
DARK_THEME_TAB_INACTIVE = 0x00262626
DARK_THEME_BORDER = 0x00454545
DARK_THEME_TAB_INACTIVE_FOREGROUND = 0x00B0B0B0

# Windows sends messages to request painting, shutdown, and theme updates.
# PeekMessage removes each message from the queue before it is dispatched.
WM_PAINT = 0x000F  # Windows is asking the window to redraw itself.
WM_QUIT = 0x0012  # The thread's message loop should end.
WM_SETTINGCHANGE = 0x001A  # A system setting, including light/dark mode, changed.
WM_THEMECHANGED = 0x031A  # The visual style changed.
PM_REMOVE = 0x0001  # Remove messages as PeekMessage reads them.
# Redrawing a region of a window *and* the children sitting in that region.
# InvalidateRect alone stops at a parent that clips its children.
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080

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
ICC_TAB_CLASSES = 0x00000008  # The tab strip.
ICC_UPDOWN_CLASS = 0x00000010  # The spin arrows beside a number box.
ICC_STANDARD_CLASSES = 0x00004000  # Button, edit, and static, themed.

# Text drawing options: center one line both horizontally and vertically and
# draw without a background rectangle.
DT_CENTER = 0x00000001  # Center text horizontally.
DT_VCENTER = 0x00000004  # Center text vertically.
DT_SINGLELINE = 0x00000020  # Keep the usage summary on one line.
TRANSPARENT_BACKGROUND = 1  # Do not let GDI paint a background behind glyphs.
DEFAULT_GUI_FONT = 17  # Windows stock font identifier for standard UI text.
# ClearType tints each letter's edge to suit one known background colour, which
# a per-pixel alpha window does not have. Grey anti-aliasing composites onto any
# background correctly, so the label asks for it in place of the default.
ANTIALIASED_QUALITY = 4

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
SETTINGS_CLASS_NAME = "ClaudeMonitorSettingsWindow"
GW_HWNDNEXT = 2  # Continue to the next sibling window.
GW_CHILD = 5  # Start at a parent's first child window.
ERROR_CLASS_ALREADY_EXISTS = 1410  # A second instance registered the class first.

# Font metrics.
SPI_GETNONCLIENTMETRICS = 0x0029  # Ask Windows for the current UI font metrics.

# Display scaling. Explorer's taskbar is per-monitor DPI aware, so a process
# that is not has every coordinate it exchanges with the taskbar virtualized:
# a child asked to occupy 180x48 arrives as 144x38 on a 125% display. Declaring
# the same awareness makes both sides speak in the same physical pixels.
USER_DEFAULT_SCREEN_DPI = 96  # The scale every design constant here is written for.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4  # Per-window DPI, updated live.
DPI_AWARENESS_UNAWARE = 0  # Coordinates are virtualized to 96 DPI.
DPI_AWARENESS_PER_MONITOR_AWARE = 2  # What Shell_TrayWnd itself reports.

# The label is drawn into an uncompressed, top-down 32bpp bitmap: one byte of
# blue, green, red and alpha per pixel, which is exactly what the compositor
# reads back out of it.
BI_RGB = 0  # Uncompressed pixel data; no compression bookkeeping needed.
DIB_RGB_COLORS = 0  # The DIB's color table holds literal RGB values (unused at 32bpp).
LABEL_BITS_PER_PIXEL = 32


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


class TCITEMW(ctypes.Structure):
    """Python layout of one tab, of which only the caption is ever set."""

    _fields_ = [
        ("mask", wintypes.UINT),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
    ]


class NMHDR(ctypes.Structure):
    """Python layout of the header every WM_NOTIFY message points at."""

    _fields_ = [
        ("hwndFrom", wintypes.HWND),
        ("idFrom", ctypes.c_void_p),
        ("code", ctypes.c_int),
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


class BLENDFUNCTION(ctypes.Structure):
    """How Windows is to combine the label's bitmap with what is behind it."""

    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),  # Always AC_SRC_OVER.
        ("BlendFlags", ctypes.c_ubyte),  # Reserved; Windows requires zero.
        ("SourceConstantAlpha", ctypes.c_ubyte),  # Whole-window opacity.
        ("AlphaFormat", ctypes.c_ubyte),  # AC_SRC_ALPHA to read per-pixel alpha.
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
        # Added in Windows Vista. SystemParametersInfoW still accepts the older
        # layout without it, but SystemParametersInfoForDpi rejects the short
        # structure outright with ERROR_INVALID_PARAMETER.
        ("iPaddedBorderWidth", ctypes.c_int),
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
    "SetForegroundWindow": ((wintypes.HWND,), wintypes.BOOL),
    "IsIconic": ((wintypes.HWND,), wintypes.BOOL),
    "EnableWindow": ((wintypes.HWND, wintypes.BOOL), wintypes.BOOL),
    "GetSystemMetrics": ((ctypes.c_int,), ctypes.c_int),
    "AdjustWindowRectEx": (
        (ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD),
        wintypes.BOOL,
    ),
    "AdjustWindowRectExForDpi": (
        (
            ctypes.POINTER(wintypes.RECT),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.UINT,
        ),
        wintypes.BOOL,
    ),
    "GetDpiForSystem": ((), wintypes.UINT),
    "FillRect": (
        (wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH),
        ctypes.c_int,
    ),
    # A one-pixel outline, which is the whole of the tab strip's line work.
    "FrameRect": (
        (wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH),
        ctypes.c_int,
    ),
    # Subclassing the tab control: its own procedure is kept and called for
    # every message but the one that paints it.
    "CallWindowProcW": (
        (
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ),
        ctypes.c_ssize_t,
    ),
    "GetMessageW": (
        (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT),
        wintypes.BOOL,
    ),
    "PostMessageW": (
        (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        wintypes.BOOL,
    ),
    "PostQuitMessage": ((ctypes.c_int,), None),
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
    "UpdateLayeredWindow": (
        (
            wintypes.HWND,
            wintypes.HDC,  # The screen the window is composited onto.
            ctypes.POINTER(wintypes.POINT),  # New screen position, or NULL.
            ctypes.POINTER(SIZE),  # New size, or NULL to keep the current one.
            wintypes.HDC,  # The memory DC holding the finished bitmap.
            ctypes.POINTER(wintypes.POINT),  # Where in that bitmap to start.
            wintypes.COLORREF,  # Unused without ULW_COLORKEY.
            ctypes.POINTER(BLENDFUNCTION),
            wintypes.DWORD,
        ),
        wintypes.BOOL,
    ),
    # Borrowing the screen's device context: CreateDIBSection and
    # UpdateLayeredWindow both want one describing the real display.
    "GetDC": ((wintypes.HWND,), wintypes.HDC),
    "ReleaseDC": ((wintypes.HWND, wintypes.HDC), ctypes.c_int),
    # Change text and dispatch Windows' message queue.
    "SetWindowTextW": ((wintypes.HWND, wintypes.LPCWSTR), wintypes.BOOL),
    "GetWindowTextW": (
        (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int),
        ctypes.c_int,
    ),
    "GetWindowTextLengthW": ((wintypes.HWND,), ctypes.c_int),
    # Tab and Escape only reach the controls if every message is offered to
    # the dialog manager first; this window is not a real dialog resource.
    "IsDialogMessageW": (
        (wintypes.HWND, ctypes.POINTER(wintypes.MSG)),
        wintypes.BOOL,
    ),
    "SetFocus": ((wintypes.HWND,), wintypes.HWND),
    "MapWindowPoints": (
        (wintypes.HWND, wintypes.HWND, ctypes.c_void_p, wintypes.UINT),
        ctypes.c_int,
    ),
    # A per-pixel alpha window is not drawn through its own device context, so
    # a paint request is answered by marking the window clean rather than by
    # painting into it. Left invalid, Windows would ask again immediately.
    "ValidateRect": ((wintypes.HWND, ctypes.c_void_p), wintypes.BOOL),
    "RedrawWindow": (
        (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
            wintypes.HANDLE,  # A region, or NULL to use the rectangle.
            wintypes.UINT,
        ),
        wintypes.BOOL,
    ),
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
    # Follow the scaling of whichever display the taskbar currently occupies.
    # These arrived in Windows 10; apply_signatures tolerates their absence.
    "GetDpiForWindow": ((wintypes.HWND,), wintypes.UINT),
    # The DPI_AWARENESS_CONTEXT values are small negative pseudo-handles, not
    # real pointers. Declaring a pointer-sized *signed* integer is what sign
    # extends -4 into the 0xFFFF...FFFC Windows actually compares against; as a
    # plain HANDLE, ctypes passes a value the call rejects.
    "SetProcessDpiAwarenessContext": ((ctypes.c_ssize_t,), wintypes.BOOL),
    "GetWindowDpiAwarenessContext": ((wintypes.HWND,), wintypes.HANDLE),
    "GetThreadDpiAwarenessContext": ((), wintypes.HANDLE),
    "GetAwarenessFromDpiAwarenessContext": ((wintypes.HANDLE,), ctypes.c_int),
    "SystemParametersInfoForDpi": (
        (wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT, wintypes.UINT),
        wintypes.BOOL,
    ),
}

GDI32_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "CreateSolidBrush": ((wintypes.COLORREF,), wintypes.HBRUSH),
    "CreateFontIndirectW": ((ctypes.POINTER(LOGFONTW),), wintypes.HGDIOBJ),
    "SetBkMode": ((wintypes.HDC, ctypes.c_int), ctypes.c_int),
    "SetBkColor": ((wintypes.HDC, wintypes.COLORREF), wintypes.COLORREF),
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
    # GDI batches drawing calls. Anything meaning to read back what it drew
    # has to flush that batch first, or it reads a surface the drawing has not
    # reached yet.
    "GdiFlush": ((), wintypes.BOOL),
    # The surface the whole label is composed on. CreateDIBSection hands back
    # both a bitmap handle for GDI and a pointer to the pixels themselves, so
    # the text GDI draws can be read back and the finished picture written in.
    "CreateDIBSection": (
        (
            wintypes.HDC,
            ctypes.POINTER(BITMAPINFO),
            wintypes.UINT,  # iUsage
            ctypes.POINTER(ctypes.c_void_p),  # Receives the address of the pixels.
            wintypes.HANDLE,  # File mapping; NULL to let Windows allocate.
            wintypes.DWORD,  # Offset into that mapping.
        ),
        wintypes.HBITMAP,
    ),
    # Releasing a font that a display-scaling change has replaced.
    "DeleteObject": ((wintypes.HGDIOBJ,), wintypes.BOOL),
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

DWMAPI_SIGNATURES: dict[str, tuple[tuple, object]] = {
    "DwmSetWindowAttribute": (
        (wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD),
        ctypes.c_long,  # HRESULT
    ),
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


def background_color_for_theme(*, uses_light_theme: bool) -> int:
    """Pick the window background the same theme calls for."""
    return LIGHT_THEME_BACKGROUND if uses_light_theme else DARK_THEME_BACKGROUND


def page_background_color_for_theme(*, uses_light_theme: bool) -> int:
    """Pick the colour of a tab page, which sits above the dialog background."""
    return (
        LIGHT_THEME_PAGE_BACKGROUND
        if uses_light_theme
        else DARK_THEME_PAGE_BACKGROUND
    )


def field_background_color_for_theme(*, uses_light_theme: bool) -> int:
    """Pick the colour inside a number box, which is lighter again."""
    return (
        LIGHT_THEME_FIELD_BACKGROUND
        if uses_light_theme
        else DARK_THEME_FIELD_BACKGROUND
    )


def rgb_from_colorref(color: int) -> tuple[int, int, int]:
    """Split a Windows COLORREF into the red, green, blue Pillow expects.

    A COLORREF stores blue in the high byte, which is the opposite of the order
    every image library uses.
    """
    return (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)


def system_uses_light_theme() -> bool:
    """Report whether Windows is currently drawing its shell in light mode."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_REGISTRY_KEY) as key:
            value, _value_type = winreg.QueryValueEx(key, _THEME_REGISTRY_VALUE)
    except OSError:
        # The value is absent on older builds, where dark taskbars are the norm.
        return False
    return bool(value)
