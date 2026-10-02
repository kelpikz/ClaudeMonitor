"""The settings dialog, drawn with Windows' own controls.

This is the second module allowed to call user32 and gdi32 directly; like
``win32_taskbar_window`` it holds only the drawing. What the window contains
is decided in ``settings.py``, and where each of those things sits is worked
out in ``settings_layout.py`` — both of which are testable without a desktop.

The shape is the one TrafficMonitor's option dialog uses: a tab strip, a page
of captioned group boxes, and OK / Cancel / Apply along the bottom. Nothing is
written as it is clicked — every change goes into the ``PendingSettings`` buffer
and reaches the real settings only on OK or Apply.

Two arrangements are worth knowing about:

- **Each page is a window, not a set of loose controls.** The three pages are
  real child windows over the tab's display area, and changing tab shows one
  and hides the others. Laying every page's controls straight onto the frame
  and hiding the inactive ones instead looks the same until the first tab
  change, when whatever the hidden controls last drew is left printed on the
  page: a hidden control cannot repaint the pixels it owns, and neither the
  frame nor a group box can be made to reach them. A page window also gives
  the page its colour, which the tab control cannot: SysTabControl32 paints
  its body in the light theme's colour whatever the system is set to.
- **The window is measured after the tab control exists.** How much of itself
  a tab control wraps around its page depends on the font it was given, so the
  frame and the tab are created at a provisional size, asked, and only then
  moved to the size the layout works out.

The pages share the frame's window class, so a message for one arrives at the
same procedure and is told apart by its handle.

In dark mode the tab control is subclassed and painted here. SysTabControl32
has no dark rendering and ignores ``DarkMode_Explorer``, so left to itself it
draws a white strip and a white line around the page on a dialog that is
otherwise near-black. The control still lays the strip out, hit-tests it, and
reports a change of tab; only its pixels are ours.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import Any, Callable

from .models import Insets, Rect
from .settings_layout import (
    _FALLBACK_TAB_FRAME,
    _MAX_SANE_TAB_INSET,
    _MIN_PAGE_WIDTH,
    _TAB_JOIN_HEIGHT,
    FieldLayout,
    GroupLayout,
    SectionLayout,
    SettingsLayout,
    TabLayout,
    settings_layout,
)
from .settings import (
    PendingSettings,
    SettingChoice,
    SettingCommand,
    SettingLink,
    SettingNumber,
    SettingOutput,
    SettingText,
    SettingToggle,
    SettingsModel,
    SettingsTab,
    WINDOW_TITLE,
    parse_number,
)
from . import icon_art, win32_icon, win32_text
from .win32_bindings import (
    BM_GETCHECK,
    BM_SETCHECK,
    BST_CHECKED,
    BST_UNCHECKED,
    BS_AUTOCHECKBOX,
    BS_DEFPUSHBUTTON,
    BS_GROUPBOX,
    BS_PUSHBUTTON,
    BUTTON_CLASS,
    CBN_SELCHANGE,
    CBS_DROPDOWNLIST,
    CB_ADDSTRING,
    CB_GETCURSEL,
    CB_SETCURSEL,
    COMBOBOX_CLASS,
    COMCTL32_SIGNATURES,
    CS_HREDRAW,
    CS_VREDRAW,
    CW_USEDEFAULT,
    DARK_MODE_COMBOBOX_THEME,
    DARK_MODE_CONTROL_THEME,
    DARK_THEME_BORDER,
    DARK_THEME_TAB_INACTIVE,
    DARK_THEME_TAB_INACTIVE_FOREGROUND,
    DT_CENTER,
    DT_SINGLELINE,
    DT_VCENTER,
    DWMAPI_SIGNATURES,
    DWMWA_USE_IMMERSIVE_DARK_MODE,
    EDIT_CLASS,
    EM_SETCUEBANNER,
    EN_CHANGE,
    ES_AUTOHSCROLL,
    ES_AUTOVSCROLL,
    ES_LEFT,
    ES_MULTILINE,
    ES_NUMBER,
    ES_READONLY,
    GDI32_SIGNATURES,
    GWLP_WNDPROC,
    HWND_BOTTOM,
    ICC_STANDARD_CLASSES,
    ICC_TAB_CLASSES,
    ICC_UPDOWN_CLASS,
    IDC_ARROW,
    IDCANCEL,
    IDOK,
    INITCOMMONCONTROLSEX,
    KERNEL32_SIGNATURES,
    LBN_SELCHANGE,
    LBS_NOINTEGRALHEIGHT,
    LBS_NOTIFY,
    LB_ADDSTRING,
    LB_GETCURSEL,
    LB_SETCURSEL,
    LISTBOX_CLASS,
    NMHDR,
    PAINTSTRUCT,
    SETTINGS_CLASS_NAME,
    SM_CXSCREEN,
    SM_CYSCREEN,
    SS_LEFT,
    STATIC_CLASS,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    SW_HIDE,
    SW_RESTORE,
    SW_SHOW,
    TAB_CONTROL_CLASS,
    TCIF_TEXT,
    TCITEMW,
    TCM_ADJUSTRECT,
    TCM_GETCURSEL,
    TCM_GETITEMCOUNT,
    TCM_GETITEMRECT,
    TCM_INSERTITEMW,
    TCN_SELCHANGE,
    TCS_FOCUSNEVER,
    TRANSPARENT_BACKGROUND,
    UDM_SETBUDDY,
    UDM_SETPOS32,
    UDM_SETRANGE32,
    UDS_ALIGNRIGHT,
    UDS_ARROWKEYS,
    UDS_NOTHOUSANDS,
    UDS_SETBUDDYINT,
    UPDOWN_CLASS,
    USER32_SIGNATURES,
    USER_DEFAULT_SCREEN_DPI,
    UXTHEME_SIGNATURES,
    WM_APP,
    WM_CLOSE,
    WM_COMMAND,
    WM_CTLCOLORBTN,
    WM_CTLCOLOREDIT,
    WM_CTLCOLORLISTBOX,
    WM_CTLCOLORSTATIC,
    WM_DESTROY,
    WM_ERASEBKGND,
    WM_NOTIFY,
    WM_PAINT,
    WM_PRINTCLIENT,
    WM_SETFONT,
    WNDCLASSEXW,
    WNDPROC,
    WS_BORDER,
    WS_CAPTION,
    WS_CHILD,
    WS_CLIPCHILDREN,
    WS_CLIPSIBLINGS,
    WS_EX_CONTROLPARENT,
    WS_GROUP,
    WS_OVERLAPPED,
    WS_SYSMENU,
    WS_TABSTOP,
    WS_VISIBLE,
    WS_VSCROLL,
    apply_signatures,
    background_color_for_theme,
    field_background_color_for_theme,
    foreground_color_for_theme,
    page_background_color_for_theme,
    system_uses_light_theme,
)

log = logging.getLogger(__name__)

# Command ids. Windows reports a click as a number, so each control needs one
# that says which field it belongs to. OK and Cancel take the ids Windows
# itself sends for Enter and Escape; the blocks below are far enough apart that
# a field's box can never be mistaken for the field itself.
_APPLY_ID = 3
_TAB_ID = 100
_FIRST_FIELD_ID = 1000
_FIRST_EDITOR_ID = 2000
_FIRST_SPINNER_ID = 3000
_FIRST_LIST_ID = 4000  # One per sectioned tab: the list down its left side.
_FIRST_ADD_ID = 5000  # The button under that list, when the tab offers one.
_UNUSED_ID = 0  # Group boxes and labels are never clicked, so never routed.

# Posted to the frame when an output box has new text to show. A manual CLI run
# finishes on a thread of its own, and only the thread that made a control may
# safely write to it, so the change is posted here rather than drawn there.
_WM_OUTPUT_CHANGED = WM_APP + 1

# How many rows an open effort list shows before it scrolls.
_CHOICE_LIST_ROWS = 8


# The window class is registered once and outlives every window made from it,
# so the procedure Windows keeps calling cannot belong to one instance: a
# settings window is built afresh each time the user opens one, and the second
# window's clicks would be delivered to the first one's dead object. The
# callback therefore routes by handle, and the reference is held here because
# Windows may call it for as long as the process lives.
_registered_wndproc_callbacks: list[object] = []
_active_windows: dict[int, "Win32SettingsWindow"] = {}
_class_registered = False

# The same arrangement for the subclassed tab controls: the callback outlives
# any one window, so it routes by handle rather than closing over an instance.
_active_tab_strips: dict[int, "Win32SettingsWindow"] = {}


@dataclass(frozen=True)
class FieldControls:
    """The handles one field's controls were created under.

    One record per field rather than a map per control: three maps keyed by the
    same field key were three chances to hold two of them and not the third.
    A toggle and a link have only ``label``; a text and a choice have a label
    and an ``editor``; a number has all three.
    """

    label: int
    editor: int | None = None
    spinner: int | None = None


@dataclass(frozen=True)
class SideList:
    """A sectioned tab's list, the section window behind each entry, and its add action."""

    handle: int
    sections: list[int]
    add: SettingLink | None = None


def _default_window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    """Give one message Windows' own handling.

    The signature is declared because several messages carry a pointer in
    lparam, and ctypes assumes a C int for an undeclared function: the pointer
    then overflows and raises inside a callback Windows is running.
    """
    user32 = ctypes.windll.user32
    apply_signatures(user32, {"DefWindowProcW": USER32_SIGNATURES["DefWindowProcW"]})
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def _dispatch(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    """Hand one message to the window it was addressed to.

    Windows sends several messages while CreateWindowExW is still running,
    before there is a handle to record, so an unknown window is normal and gets
    the default handling.
    """
    window = _active_windows.get(hwnd)
    if window is None:
        return _default_window_proc(hwnd, message, wparam, lparam)
    return window._window_proc(hwnd, message, wparam, lparam)


# ----------------------------------------------------------------------- layout


def _dispatch_tab_strip(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    """Hand one tab control message to the settings window that subclassed it."""
    window = _active_tab_strips.get(hwnd)
    if window is None:
        return _default_window_proc(hwnd, message, wparam, lparam)
    return window._tab_strip_proc(hwnd, message, wparam, lparam)



# ----------------------------------------------------------------------- window


class Win32SettingsWindow:
    """One settings dialog: created, pumped, and destroyed on the caller's thread."""

    def __init__(
        self,
        model: SettingsModel,
        *,
        uses_light_theme: Callable[[], bool] = system_uses_light_theme,
    ) -> None:
        self._model = model
        self._fields = model.fields()
        self._pending = PendingSettings(model)
        self._uses_light_theme = uses_light_theme
        self._user32 = self._load("user32", USER32_SIGNATURES)
        self._gdi32 = self._load("gdi32", GDI32_SIGNATURES)
        self._kernel32 = self._load("kernel32", KERNEL32_SIGNATURES)
        self._uxtheme = self._load("uxtheme", UXTHEME_SIGNATURES)
        self._dwmapi = self._load("dwmapi", DWMAPI_SIGNATURES)
        self._comctl32 = self._load("comctl32", COMCTL32_SIGNATURES)
        self._handle: int | None = None
        self._tab_handle: int | None = None
        self._page_handles: list[int] = []
        self._side_lists: list[SideList] = []
        self._font: int | None = None
        self._font_is_stock = False
        # The Last run box shows a command line, in a fixed-width face.
        self._monospace_font: int | None = None
        # Each group that holds only output boxes, and the caption and border
        # that are hidden with it while every one of those boxes is empty.
        self._output_groups: list[tuple[list[SettingOutput], list[int]]] = []
        self._background_brush: int | None = None
        self._page_brush: int | None = None
        self._field_brush: int | None = None
        self._tab_inactive_brush: int | None = None
        self._border_brush: int | None = None
        # The title bar and taskbar icons, freed when the window closes.
        self._icons: list[int] = []
        self._tab_frame = _FALLBACK_TAB_FRAME
        self._original_tab_proc: int | None = None
        self._foreground_color = 0
        self._controls: dict[str, FieldControls] = {}
        # The stored value behind each entry of each drop-down list, by field key.
        self._choice_values: dict[str, list[str]] = {}
        # Set while the window writes a value into a box itself, so the change
        # notification that causes is not mistaken for something the user typed.
        self._syncing = False

    @staticmethod
    def _load(name: str, signatures: dict) -> Any:
        """Load one DLL with its argument types declared."""
        dll = ctypes.WinDLL(name, use_last_error=True)
        missing = apply_signatures(dll, signatures)
        if missing:
            log.warning("%s does not export %s on this build", name, ", ".join(missing))
        return dll

    # ---------------------------------------------------------------- protocol

    def show(self) -> None:
        """Create the window and return only once the user has closed it."""
        try:
            self._create()
            self._pump()
        finally:
            sections = [handle for side in self._side_lists for handle in side.sections]
            for handle in [*sections, *self._page_handles, self._handle]:
                if handle is not None:
                    _active_windows.pop(handle, None)
            self._page_handles = []
            self._side_lists = []
            if self._tab_handle is not None:
                _active_tab_strips.pop(self._tab_handle, None)
            self._release_resources()
            self._handle = None

    def focus(self) -> bool:
        """Bring an open window forward, reporting whether Windows agreed."""
        if self._handle is None:
            return False
        # A minimised window comes back to the size it had, not to an icon.
        self._user32.ShowWindow(self._handle, SW_RESTORE)
        return bool(self._user32.SetForegroundWindow(self._handle))

    def close(self) -> None:
        """Ask the window to close itself, from whichever thread is shutting down.

        PostMessage rather than DestroyWindow: only the thread that created a
        window may destroy it, and this is called from the one that did not.
        """
        if self._handle is None:
            return
        self._user32.PostMessageW(self._handle, WM_CLOSE, 0, 0)

    # ----------------------------------------------------------------- creation

    def _create(self) -> int:
        """Build the frame, then the tab strip, then everything the pages hold."""
        light = self._uses_light_theme()
        self._create_brushes(light)
        self._register_common_controls()
        self._register_class()

        dpi = self._system_dpi()
        self._font, self._font_is_stock = win32_text.create_message_font(
            self._user32, self._gdi32, dpi
        )
        self._monospace_font, _ = win32_text.create_monospace_font(
            self._user32, self._gdi32, dpi
        )
        self._handle = self._create_frame()
        _active_windows[self._handle] = self
        self._set_icons()
        self._apply_dark_title_bar(light)

        self._tab_handle = self._create_tab_strip()
        self._subclass_tab_strip(light)
        layout = self._layout(dpi)
        self._resize_to(layout)
        self._user32.SetWindowPos(
            self._tab_handle,
            None,
            layout.tab.left,
            layout.tab.top,
            layout.tab.width,
            layout.tab.height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        self._create_pages(layout)
        self._create_dialog_buttons(layout)
        self._stack_tab_behind_its_pages()
        self._show_tab(0)

        self._user32.ShowWindow(self._handle, SW_SHOW)
        self._user32.SetForegroundWindow(self._handle)
        return self._handle

    def _set_icons(self) -> None:
        """Give the frame the app's own icon, for the title bar and the taskbar.

        The class names no icon, so without this Windows draws a blank one in
        the caption, and the taskbar shows python.exe's under `uv run dev`.
        """
        self._icons = win32_icon.set_window_icons(
            self._user32, self._handle, icon_art.application_icon_png
        )

    def _stack_tab_behind_its_pages(self) -> None:
        """Put the tab control at the back, behind the pages it frames.

        A child created later goes to the *bottom* of the z-order, so the tab
        control starts out in front of every page window and hides them.
        """
        self._user32.SetWindowPos(
            self._tab_handle,
            HWND_BOTTOM,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def _create_brushes(self, light: bool) -> None:
        """Make the three surfaces the dialog paints: frame, page, and box."""
        self._foreground_color = foreground_color_for_theme(uses_light_theme=light)
        self._background_brush = self._gdi32.CreateSolidBrush(
            background_color_for_theme(uses_light_theme=light)
        )
        self._page_brush = self._gdi32.CreateSolidBrush(
            page_background_color_for_theme(uses_light_theme=light)
        )
        self._field_brush = self._gdi32.CreateSolidBrush(
            field_background_color_for_theme(uses_light_theme=light)
        )
        if light:
            # A light tab strip is Windows' own, and better than anything drawn
            # here; only the dark one has to be painted, so only it needs these.
            return
        self._tab_inactive_brush = self._gdi32.CreateSolidBrush(DARK_THEME_TAB_INACTIVE)
        self._border_brush = self._gdi32.CreateSolidBrush(DARK_THEME_BORDER)

    def _register_common_controls(self) -> None:
        """Ask Windows for the tab strip, the spin arrows, and the themed basics."""
        controls = INITCOMMONCONTROLSEX()
        controls.dwSize = ctypes.sizeof(INITCOMMONCONTROLSEX)
        controls.dwICC = ICC_TAB_CLASSES | ICC_UPDOWN_CLASS | ICC_STANDARD_CLASSES
        try:
            if not self._comctl32.InitCommonControlsEx(ctypes.byref(controls)):
                log.warning("Windows declined to register the tab and spin controls")
        except (AttributeError, OSError) as exc:
            log.warning("unable to register the common controls (%s)", exc)

    def _create_frame(self) -> int:
        """Create the caption-only dialog frame, at a size the layout replaces.

        It has to exist before the tab control, and the tab control has to
        exist before its size can be worked out, so this opens hidden and small.
        """
        return self._user32.CreateWindowExW(
            WS_EX_CONTROLPARENT,
            SETTINGS_CLASS_NAME,
            WINDOW_TITLE,
            self._frame_style(),
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            _MIN_PAGE_WIDTH,
            _MIN_PAGE_WIDTH,
            None,
            None,
            self._kernel32.GetModuleHandleW(None),
            None,
        )

    @staticmethod
    def _frame_style() -> int:
        """Return the style of a fixed dialog that clips its own children."""
        return WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_CLIPCHILDREN

    def _create_tab_strip(self) -> int:
        """Create the tab control and give it one tab per page."""
        handle = self._user32.CreateWindowExW(
            0,
            TAB_CONTROL_CLASS,
            "",
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | TCS_FOCUSNEVER,
            0,
            0,
            _MIN_PAGE_WIDTH,
            _MIN_PAGE_WIDTH,
            self._handle,
            _TAB_ID,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        self._user32.SendMessageW(handle, WM_SETFONT, self._font, 1)
        self._apply_dark_control_theme(handle)
        for index, tab in enumerate(self._model.tabs):
            self._insert_tab(handle, index, tab.title)
        return handle

    def _subclass_tab_strip(self, light: bool) -> None:
        """Take over the tab control's painting, where Windows will not do it.

        Only the painting: every other message is handed straight back to the
        control, which keeps its own layout, hit-testing, and selection.
        """
        if light:
            return
        callback = WNDPROC(_dispatch_tab_strip)
        _registered_wndproc_callbacks.append(callback)
        _active_tab_strips[self._tab_handle] = self
        self._original_tab_proc = self._user32.SetWindowLongPtrW(
            self._tab_handle,
            GWLP_WNDPROC,
            ctypes.cast(callback, ctypes.c_void_p).value,
        )

    def _tab_strip_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Paint the strip; leave everything else to the control itself."""
        if message == WM_ERASEBKGND:
            return 1  # The paint below covers every pixel of it.
        if message == WM_PAINT:
            return self._on_tab_strip_paint(hwnd)
        return self._call_original_tab_proc(hwnd, message, wparam, lparam)

    def _call_original_tab_proc(
        self, hwnd: int, message: int, wparam: int, lparam: int
    ) -> int:
        """Give one message back to the tab control's own procedure."""
        if self._original_tab_proc is None:
            return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)
        return self._user32.CallWindowProcW(
            self._original_tab_proc, hwnd, message, wparam, lparam
        )

    def _on_tab_strip_paint(self, hwnd: int) -> int:
        """Answer one paint request for the tab strip."""
        paint = PAINTSTRUCT()
        device_context = self._user32.BeginPaint(hwnd, ctypes.byref(paint))
        try:
            self._paint_tab_strip(device_context)
        finally:
            self._user32.EndPaint(hwnd, ctypes.byref(paint))
        return 0

    def _paint_tab_strip(self, device_context: int) -> None:
        """Draw the whole strip: the tabs, their titles, and the page's outline."""
        client = self._client_rect(self._tab_handle)
        self._user32.FillRect(
            device_context, ctypes.byref(client), self._background_brush
        )

        previous_font = self._gdi32.SelectObject(device_context, self._font)
        self._gdi32.SetBkMode(device_context, TRANSPARENT_BACKGROUND)
        selected = self._selected_tab_index()
        for index in range(self._tab_count()):
            self._paint_one_tab(device_context, index, selected)
        self._gdi32.SelectObject(device_context, previous_font)

        page = self._page_outline(client)
        self._user32.FrameRect(
            device_context, ctypes.byref(page), self._border_brush
        )
        self._join_current_tab_to_page(device_context, selected, page)

    def _paint_one_tab(self, device_context: int, index: int, selected: int) -> None:
        """Fill one tab, outline it, and print its title."""
        rect = self._tab_item_rect(index)
        current = index == selected
        self._user32.FillRect(
            device_context,
            ctypes.byref(rect),
            self._page_brush if current else self._tab_inactive_brush,
        )
        self._user32.FrameRect(
            device_context, ctypes.byref(rect), self._border_brush
        )
        self._gdi32.SetTextColor(
            device_context,
            self._foreground_color if current else DARK_THEME_TAB_INACTIVE_FOREGROUND,
        )
        title = self._model.tabs[index].title if index < len(self._model.tabs) else ""
        self._user32.DrawTextW(
            device_context,
            title,
            len(title),
            ctypes.byref(rect),
            DT_CENTER | DT_VCENTER | DT_SINGLELINE,
        )

    def _join_current_tab_to_page(
        self, device_context: int, selected: int, page: wintypes.RECT
    ) -> None:
        """Erase the page outline under the current tab, so the two read as one."""
        if not 0 <= selected < self._tab_count():
            return
        tab = self._tab_item_rect(selected)
        join = wintypes.RECT(
            tab.left + 1,
            page.top - _TAB_JOIN_HEIGHT,
            max(tab.left + 1, tab.right - 1),
            page.top + _TAB_JOIN_HEIGHT,
        )
        self._user32.FillRect(device_context, ctypes.byref(join), self._page_brush)

    def _page_outline(self, client: wintypes.RECT) -> wintypes.RECT:
        """Return the line that goes around the page, inside the tab control."""
        return wintypes.RECT(
            client.left,
            client.top + self._tab_frame.top - 1,
            client.right,
            client.bottom,
        )

    def _tab_item_rect(self, index: int) -> wintypes.RECT:
        """Ask the control where it has put one tab."""
        rect = wintypes.RECT()
        self._user32.SendMessageW(
            self._tab_handle, TCM_GETITEMRECT, index, ctypes.addressof(rect)
        )
        return rect

    def _tab_count(self) -> int:
        """Ask the control how many tabs it is showing."""
        return int(self._user32.SendMessageW(self._tab_handle, TCM_GETITEMCOUNT, 0, 0))

    def _client_rect(self, window: int) -> wintypes.RECT:
        """Return one window's client rectangle."""
        rect = wintypes.RECT()
        self._user32.GetClientRect(window, ctypes.byref(rect))
        return rect

    def _insert_tab(self, handle: int, index: int, title: str) -> None:
        """Add one caption to the tab strip."""
        item = TCITEMW()
        item.mask = TCIF_TEXT
        item.pszText = ctypes.cast(
            ctypes.create_unicode_buffer(title), wintypes.LPWSTR
        )
        self._user32.SendMessageW(
            handle, TCM_INSERTITEMW, index, ctypes.addressof(item)
        )

    def _layout(self, dpi: int) -> SettingsLayout:
        """Measure the window's own text and lay the controls out around it."""
        self._tab_frame = self._measure_tab_frame()
        return settings_layout(
            self._model,
            measure=self._text_width,
            tab_frame=self._tab_frame,
            dpi=dpi,
        )

    def _measure_tab_frame(self) -> Insets:
        """Ask the tab control how much of itself surrounds the page it shows.

        A refusal, or an answer that cannot be right, leaves the fallback
        standing: a page a few pixels out is far better than no dialog at all.
        """
        probe = wintypes.RECT(0, 0, 1000, 1000)
        try:
            self._user32.SendMessageW(
                self._tab_handle, TCM_ADJUSTRECT, 1, ctypes.addressof(probe)
            )
        except (AttributeError, OSError) as exc:
            log.warning("unable to measure the tab control (%s)", exc)
            return _FALLBACK_TAB_FRAME
        insets = Insets(
            -probe.left, -probe.top, probe.right - 1000, probe.bottom - 1000
        )
        if not self._is_sane_frame(insets):
            return _FALLBACK_TAB_FRAME
        return insets

    @staticmethod
    def _is_sane_frame(insets: Insets) -> bool:
        """Report whether a measured tab frame could plausibly be real."""
        return all(
            0 <= edge <= _MAX_SANE_TAB_INSET
            for edge in (insets.left, insets.top, insets.right, insets.bottom)
        )

    def _resize_to(self, layout: SettingsLayout) -> None:
        """Grow the frame to the client size the layout needs, and centre it."""
        dpi = self._system_dpi()
        outer = self._frame_size(layout, self._frame_style(), dpi)
        x, y = self._centred_position(outer)
        self._user32.SetWindowPos(
            self._handle, None, x, y, outer[0], outer[1], SWP_NOZORDER
        )

    def _frame_size(
        self, layout: SettingsLayout, style: int, dpi: int
    ) -> tuple[int, int]:
        """Grow the client size by whatever the caption and borders need.

        The border is itself scaled, so the per-DPI variant is asked first; it
        arrived in Windows 10 1607 and older builds fall back to the plain one.
        """
        rect = wintypes.RECT(0, 0, layout.width, layout.height)
        try:
            self._user32.AdjustWindowRectExForDpi(
                ctypes.byref(rect), style, False, 0, dpi
            )
        except (AttributeError, OSError):
            self._user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        return rect.right - rect.left, rect.bottom - rect.top

    def _centred_position(self, outer: tuple[int, int]) -> tuple[int, int]:
        """Place the window in the middle of the primary display."""
        screen_width = self._user32.GetSystemMetrics(SM_CXSCREEN)
        screen_height = self._user32.GetSystemMetrics(SM_CYSCREEN)
        if not screen_width or not screen_height:
            return CW_USEDEFAULT, CW_USEDEFAULT
        return (screen_width - outer[0]) // 2, (screen_height - outer[1]) // 2

    def _text_width(self, text: str) -> int:
        """Return the width the dialog font renders one label at."""
        return win32_text.measure_text_width(self._gdi32, self._font, text)

    def _system_dpi(self) -> int:
        """Return the scaling of the display the window will be centred on.

        The dialog is per-monitor DPI aware because the process is, so nothing
        scales it for us: at 150% a window laid out for 96 DPI would open at
        two thirds of its intended size, with a font to match.
        """
        try:
            return self._user32.GetDpiForSystem() or USER_DEFAULT_SCREEN_DPI
        except (AttributeError, OSError):
            return USER_DEFAULT_SCREEN_DPI

    # ------------------------------------------------------------ page controls

    def _create_pages(self, layout: SettingsLayout) -> None:
        """Create one window per tab and fill it with that tab's controls."""
        indexed = {field.key: index for index, field in enumerate(self._fields)}
        self._page_handles = [
            self._create_page(tab, placed, layout.page, indexed)
            for tab, placed in zip(self._model.tabs, layout.tabs)
        ]

    def _create_page(
        self, tab: SettingsTab, placed: TabLayout, page: Rect, indexed: dict[str, int]
    ) -> int:
        """Create one page window over the tab's display area, and its contents."""
        handle = self._create_child_window(page, self._handle)
        self._create_groups(placed.groups, handle, indexed)
        if placed.side_list is not None:
            self._create_side_list(tab, placed, handle, indexed)
        return handle

    def _create_child_window(self, rect: Rect, parent: int) -> int:
        """Create one plain window of our own class: a page, or a section on a page.

        It shares the frame's class, so its own messages — the colour requests
        from its controls, and their clicks — arrive at the same procedure.
        """
        handle = self._user32.CreateWindowExW(
            WS_EX_CONTROLPARENT,
            SETTINGS_CLASS_NAME,
            "",
            # No WS_CLIPCHILDREN: the page has to paint underneath its own
            # controls, because a themed group box or checkbox draws no
            # background of its own and would otherwise leave the frame's
            # colour showing through the middle of the page.
            WS_CHILD | WS_CLIPSIBLINGS,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            parent,
            _UNUSED_ID,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        _active_windows[handle] = self
        self._apply_dark_control_theme(handle)
        return handle

    def _create_groups(
        self, groups: list[GroupLayout], parent: int, indexed: dict[str, int]
    ) -> None:
        """Create every group box on one window, and the fields inside each."""
        for group in groups:
            # The fields come first: a group box fills its own interior with
            # the colour it is given, and a child made later sits lower in the
            # z-order, which is what leaves the box behind what it surrounds.
            items = [self._fields[indexed[placed.key]] for placed in group.fields]
            for item, placed in zip(items, group.fields):
                self._create_field(item, placed, indexed[placed.key], parent)
            frame = self._create_group_box(group, parent)
            if items and all(isinstance(item, SettingOutput) for item in items):
                self._output_groups.append((items, frame))
                self._show_output_group(items, frame)

    def _create_side_list(
        self, tab: SettingsTab, placed: TabLayout, page: int, indexed: dict[str, int]
    ) -> None:
        """Create a sectioned tab's list, one window per section, and the add button.

        Each section is a window for the reason each page is one: hidden loose
        controls leave the last section printed through the next.
        """
        list_index = len(self._side_lists)
        listbox = self._create_control(
            LISTBOX_CLASS,
            "",
            LBS_NOTIFY | LBS_NOINTEGRALHEIGHT | WS_VSCROLL | WS_BORDER | WS_TABSTOP,
            placed.side_list,
            _FIRST_LIST_ID + list_index,
            page,
        )
        for section in placed.sections:
            text = ctypes.create_unicode_buffer(section.title)
            self._user32.SendMessageW(listbox, LB_ADDSTRING, 0, ctypes.addressof(text))
        if placed.add_button is not None and tab.add_section is not None:
            self._create_control(
                BUTTON_CLASS,
                tab.add_section.label,
                BS_PUSHBUTTON | WS_TABSTOP,
                placed.add_button,
                _FIRST_ADD_ID + list_index,
                page,
            )
        side = SideList(
            handle=listbox,
            sections=[self._create_section(section, page, indexed) for section in placed.sections],
            add=tab.add_section,
        )
        self._side_lists.append(side)
        self._user32.SendMessageW(listbox, LB_SETCURSEL, 0, 0)
        self._show_section(side, 0)

    def _create_section(
        self, section: SectionLayout, page: int, indexed: dict[str, int]
    ) -> int:
        """Create one section window beside the list, and the groups it holds."""
        handle = self._create_child_window(section.rect, page)
        self._create_groups(section.groups, handle, indexed)
        return handle

    def _create_group_box(self, group: GroupLayout, parent: int) -> list[int]:
        """Create one box around a set of related fields, and caption it.

        The caption is a static of our own rather than the group box's title,
        because a group box paints its title in whichever colour the visual
        style picks — which on a dark page is black on near-black. The static
        is created first, so it sits above the border it interrupts. Both
        handles are returned, caption first, so the pair can be hidden together.
        """
        caption = self._create_control(
            STATIC_CLASS, group.title, SS_LEFT, group.caption, _UNUSED_ID, parent
        )
        box = self._create_control(
            BUTTON_CLASS,
            "",
            BS_GROUPBOX | WS_GROUP,
            group.rect,
            _UNUSED_ID,
            parent,
        )
        return [caption, box]

    def _create_field(
        self, item, placed: FieldLayout, index: int, parent: int
    ) -> list[int]:
        """Create the controls one field needs, and remember how to reach them.

        The index is the field's position in ``self._fields``, which is what a
        click is routed back by. It is passed in rather than looked up: two
        fields wired the same way are equal frozen dataclasses, so a search
        would find the first of them for both.
        """
        if isinstance(item, SettingToggle):
            return [self._create_checkbox(item, placed, index, parent)]
        if isinstance(item, SettingNumber):
            return self._create_number(item, placed, index, parent)
        if isinstance(item, SettingText):
            return self._create_text(item, placed, index, parent)
        if isinstance(item, SettingChoice):
            return self._create_choice(item, placed, index, parent)
        if isinstance(item, SettingOutput):
            return [self._create_output(item, placed, parent)]
        return [
            self._create_control(
                BUTTON_CLASS,
                item.label,
                BS_PUSHBUTTON | WS_TABSTOP,
                placed.rect,
                _FIRST_FIELD_ID + index,
                parent,
            )
        ]

    def _create_checkbox(self, item, placed: FieldLayout, index: int, parent: int) -> int:
        """Create one checkbox, ticked as the setting currently reads."""
        handle = self._create_control(
            BUTTON_CLASS,
            item.label,
            BS_AUTOCHECKBOX | WS_TABSTOP,
            placed.rect,
            _FIRST_FIELD_ID + index,
            parent,
        )
        self._controls[item.key] = FieldControls(label=handle)
        self._set_checked(handle, bool(self._pending.value_of(item.key)))
        if not self._reads_available(item):
            self._user32.EnableWindow(handle, False)
        return handle

    def _create_number(self, item, placed: FieldLayout, index: int, parent: int) -> list[int]:
        """Create a number's label, its box, its spin arrows, and its unit."""
        label = self._create_control(
            STATIC_CLASS, item.label, SS_LEFT, placed.rect, _UNUSED_ID, parent
        )
        self._controls[item.key] = FieldControls(label=label)
        editor = self._create_control(
            EDIT_CLASS,
            str(self._pending.value_of(item.key)),
            ES_LEFT | ES_NUMBER | ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP,
            placed.editor,
            _FIRST_EDITOR_ID + index,
            parent,
        )
        self._controls[item.key] = FieldControls(label=label, editor=editor)
        spinner = self._create_spinner(item, placed, index, editor, parent)
        suffix = self._create_control(
            STATIC_CLASS, item.suffix, SS_LEFT, placed.suffix, _UNUSED_ID, parent
        )
        return [label, editor, spinner, suffix]

    def _create_text(self, item, placed: FieldLayout, index: int, parent: int) -> list[int]:
        """Create a text field's label and the box it is typed into."""
        label = self._create_control(
            STATIC_CLASS, item.label, SS_LEFT, placed.rect, _UNUSED_ID, parent
        )
        editor = self._create_control(
            EDIT_CLASS,
            str(self._pending.value_of(item.key) or ""),
            ES_LEFT | ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP,
            placed.editor,
            _FIRST_EDITOR_ID + index,
            parent,
        )
        self._controls[item.key] = FieldControls(label=label, editor=editor)
        placeholder = item.placeholder()
        if placeholder:
            cue = ctypes.create_unicode_buffer(placeholder)
            # wparam 1 keeps the grey text while the empty box has the focus.
            self._user32.SendMessageW(editor, EM_SETCUEBANNER, 1, ctypes.addressof(cue))
        return [label, editor]

    def _create_choice(self, item, placed: FieldLayout, index: int, parent: int) -> list[int]:
        """Create a choice's label and its drop-down list, on the stored entry.

        A combo box is created as tall as its open list; the closed box is
        drawn at the row's own height.
        """
        label = self._create_control(
            STATIC_CLASS, item.label, SS_LEFT, placed.rect, _UNUSED_ID, parent
        )
        box = placed.editor
        combo = self._create_control(
            COMBOBOX_CLASS,
            "",
            CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP,
            Rect(box.left, box.top, box.right, box.top + box.height * _CHOICE_LIST_ROWS),
            _FIRST_FIELD_ID + index,
            parent,
        )
        if not self._uses_light_theme():
            self._set_theme(combo, DARK_MODE_COMBOBOX_THEME)
        self._controls[item.key] = FieldControls(label=label, editor=combo)
        self._fill_choices(item, combo)
        return [label, combo]

    def _create_output(self, item, placed: FieldLayout, parent: int) -> int:
        """Create a read-only box of several lines, showing its text as it reads now.

        It is never typed into, so it is never routed: its id is the unused one.
        """
        handle = self._create_control(
            EDIT_CLASS,
            self._output_text(item),
            ES_LEFT | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | WS_VSCROLL | WS_BORDER,
            placed.rect,
            _UNUSED_ID,
            parent,
        )
        self._controls[item.key] = FieldControls(label=handle)
        self._user32.SendMessageW(handle, WM_SETFONT, self._monospace_font, 1)
        self._set_shown(handle, bool(self._output_text(item)))
        return handle

    def _set_shown(self, handle: int, shown: bool) -> None:
        """Show or hide one control."""
        self._user32.ShowWindow(handle, SW_SHOW if shown else SW_HIDE)

    def _show_output_group(self, outputs: list, frame: list[int]) -> None:
        """Show a group of output boxes only while one of them has something to say."""
        shown = any(self._output_text(item) for item in outputs)
        for handle in frame:
            self._set_shown(handle, shown)

    def _output_text(self, item) -> str:
        """Read one output box's text, with the line breaks an edit control needs."""
        try:
            text = str(item.text())
        except Exception:
            log.exception("unable to read the %r output", item.key)
            return ""
        return "\r\n".join(text.splitlines())

    def _refresh_outputs(self) -> None:
        """Show what every output box now says, and hide each one with nothing to say."""
        for item in self._fields:
            if isinstance(item, SettingOutput) and item.key in self._controls:
                handle = self._controls[item.key].label
                text = self._output_text(item)
                self._user32.SetWindowTextW(handle, text)
                self._set_shown(handle, bool(text))
        for outputs, frame in self._output_groups:
            self._show_output_group(outputs, frame)

    def _fill_choices(self, item, combo: int) -> None:
        """Add every choice to the list and select the stored one.

        A stored value the list does not offer — a hand-edited config, say —
        is added as an entry of its own rather than shown as nothing.
        """
        entries = list(item.choices)
        current = str(self._pending.value_of(item.key) or "")
        if current not in [value for value, _label in entries]:
            entries.append((current, current))
        self._choice_values[item.key] = [value for value, _label in entries]
        for _value, shown in entries:
            text = ctypes.create_unicode_buffer(shown)
            self._user32.SendMessageW(combo, CB_ADDSTRING, 0, ctypes.addressof(text))
        self._select_choice(item)

    def _select_choice(self, item) -> None:
        """Select the entry that holds the setting's current value."""
        values = self._choice_values.get(item.key, [])
        current = str(self._pending.value_of(item.key) or "")
        index = values.index(current) if current in values else -1
        self._user32.SendMessageW(self._controls[item.key].editor, CB_SETCURSEL, index, 0)

    def _create_spinner(
        self, item, placed: FieldLayout, index: int, editor: int, parent: int
    ) -> int:
        """Create the arrows beside one number box and tie them to it."""
        handle = self._create_control(
            UPDOWN_CLASS,
            "",
            UDS_ALIGNRIGHT | UDS_SETBUDDYINT | UDS_ARROWKEYS | UDS_NOTHOUSANDS,
            placed.spinner,
            _FIRST_SPINNER_ID + index,
            parent,
        )
        self._controls[item.key] = replace(self._controls[item.key], spinner=handle)
        self._user32.SendMessageW(handle, UDM_SETBUDDY, editor, 0)
        self._user32.SendMessageW(handle, UDM_SETRANGE32, item.minimum, item.maximum)
        self._user32.SendMessageW(
            handle, UDM_SETPOS32, 0, int(self._pending.value_of(item.key) or 0)
        )
        return handle

    def _create_dialog_buttons(self, layout: SettingsLayout) -> None:
        """Create OK, Cancel, and Apply on the frame below the tab control."""
        self._create_control(
            BUTTON_CLASS, "OK", BS_DEFPUSHBUTTON | WS_TABSTOP, layout.ok, IDOK
        )
        self._create_control(
            BUTTON_CLASS, "Cancel", BS_PUSHBUTTON | WS_TABSTOP, layout.cancel, IDCANCEL
        )
        self._create_control(
            BUTTON_CLASS, "Apply", BS_PUSHBUTTON | WS_TABSTOP, layout.apply, _APPLY_ID
        )

    def _create_control(
        self,
        class_name: str,
        label: str,
        style: int,
        rect: Rect,
        control_id: int,
        parent: int | None = None,
    ) -> int:
        """Create one child control and give it the dialog's font and theme."""
        handle = self._user32.CreateWindowExW(
            0,
            class_name,
            label,
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | style,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            self._handle if parent is None else parent,
            control_id,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        self._user32.SendMessageW(handle, WM_SETFONT, self._font, 1)
        self._apply_dark_control_theme(handle)
        return handle

    def _apply_dark_title_bar(self, light: bool) -> None:
        """Ask the Desktop Window Manager for a dark caption, where it is offered.

        Older builds answer with a failure HRESULT, which is the whole of the
        handling needed: the window simply keeps a light title bar.
        """
        if light:
            return
        try:
            enabled = wintypes.BOOL(True)
            self._dwmapi.DwmSetWindowAttribute(
                self._handle,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
        except (AttributeError, OSError) as exc:
            log.warning("unable to request a dark title bar (%s)", exc)

    def _apply_dark_control_theme(self, handle: int) -> None:
        """Give one control the dark visual style File Explorer uses."""
        if self._uses_light_theme():
            return
        self._set_theme(handle, DARK_MODE_CONTROL_THEME)

    def _set_theme(self, handle: int, theme: str) -> None:
        """Give one control a named visual style, logging a refusal."""
        try:
            self._uxtheme.SetWindowTheme(handle, theme, None)
        except (AttributeError, OSError) as exc:
            log.warning("unable to apply the %s theme (%s)", theme, exc)

    def _register_class(self) -> None:
        """Register the settings window class once per process."""
        global _class_registered
        if _class_registered:
            return
        callback = WNDPROC(_dispatch)
        _registered_wndproc_callbacks.append(callback)

        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(WNDCLASSEXW)
        window_class.style = CS_HREDRAW | CS_VREDRAW
        window_class.lpfnWndProc = callback
        window_class.hInstance = self._kernel32.GetModuleHandleW(None)
        window_class.hCursor = self._user32.LoadCursorW(None, IDC_ARROW)
        # The background is painted by WM_ERASEBKGND instead, because the colour
        # follows a theme the user can change while the class cannot.
        window_class.hbrBackground = None
        window_class.lpszClassName = SETTINGS_CLASS_NAME
        self._user32.RegisterClassExW(ctypes.byref(window_class))
        _class_registered = True

    # ---------------------------------------------------------------- messages

    def _pump(self) -> None:
        """Dispatch messages until the window is destroyed.

        Every message is offered to the dialog manager first, which is what
        makes Tab walk the controls and Escape mean Cancel; this window is a
        plain class rather than a dialog resource, so nothing does that for us.

        GetMessage answers 0 for WM_QUIT and -1 for an error — an invalid
        handle, say. Treating -1 as "a message arrived" would spin this thread
        at full speed for as long as the app ran, so both end the loop.
        """
        message = wintypes.MSG()
        while True:
            received = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if received in (0, -1):
                if received == -1:
                    log.warning("settings window message pump failed; closing it")
                return
            if self._handled_as_dialog_key(message):
                continue
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def _handled_as_dialog_key(self, message) -> bool:
        """Let the dialog manager have Tab, Escape, and Enter first."""
        if self._handle is None:
            return False
        try:
            return bool(
                self._user32.IsDialogMessageW(self._handle, ctypes.byref(message))
            )
        except (AttributeError, OSError):
            return False

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Handle the few messages a dialog of this shape actually receives.

        The pages share this class, so a message that arrives for one of them
        is told apart by its handle and answered separately.
        """
        if hwnd != self._handle:
            return self._page_proc(hwnd, message, wparam, lparam)
        if message == _WM_OUTPUT_CHANGED:
            self._refresh_outputs()
            return 0
        if message == WM_COMMAND:
            return self._on_command(hwnd, wparam & 0xFFFF, (wparam >> 16) & 0xFFFF)
        if message == WM_NOTIFY:
            return self._on_notify(lparam)
        if message == WM_ERASEBKGND:
            return self._paint_background(wparam)
        if message in (WM_CTLCOLOREDIT, WM_CTLCOLORLISTBOX):
            return self._color_editor(wparam)
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
            return self._color_dialog_control(wparam)
        if message == WM_CLOSE:
            # The title bar's close button means the same thing Cancel does.
            self._pending.discard()
            self._user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _page_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Answer for one page window: its background, and its controls.

        A control sends its clicks and its colour requests to its own parent,
        which is the page rather than the frame; the clicks are handed on so
        there is still one place that knows what every control means.
        """
        if message == WM_COMMAND:
            return self._on_command(
                self._handle, wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
            )
        if message in (WM_ERASEBKGND, WM_PRINTCLIENT):
            # A themed group box or checkbox paints no background of its own.
            # It asks its parent to paint one behind it, through WM_PRINTCLIENT
            # and its own device context; left unanswered, the control falls
            # back to the dialog grey and prints a hole in the page.
            return self._fill(wparam, hwnd, self._page_brush)
        if message in (WM_CTLCOLOREDIT, WM_CTLCOLORLISTBOX):
            return self._color_editor(wparam)
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
            return self._color_page_control(wparam)
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _on_command(self, hwnd: int, control_id: int, notification: int) -> int:
        """Route a click, a pick, typed text, or a dialog button to what owns it."""
        field_index = control_id - _FIRST_FIELD_ID
        if 0 <= field_index < len(self._fields):
            self._field_used(field_index, notification)
            return 0

        editor_index = control_id - _FIRST_EDITOR_ID
        if notification == EN_CHANGE and 0 <= editor_index < len(self._fields):
            self._editor_typed(editor_index)
            return 0

        list_index = control_id - _FIRST_LIST_ID
        if 0 <= list_index < len(self._side_lists):
            if notification == LBN_SELCHANGE:
                self._section_picked(self._side_lists[list_index])
            return 0

        add_index = control_id - _FIRST_ADD_ID
        if 0 <= add_index < len(self._side_lists):
            add = self._side_lists[add_index].add
            if add is not None:
                self._open_link(add)
            return 0

        if control_id == IDOK:
            self._pending.apply()
            self._user32.DestroyWindow(hwnd)
            return 0
        if control_id == IDCANCEL:
            self._pending.discard()
            self._user32.DestroyWindow(hwnd)
            return 0
        if control_id == _APPLY_ID:
            self._pending.apply()
            self._refresh_controls()
            return 0
        return 0

    def _on_notify(self, lparam: int) -> int:
        """Answer the one notification this dialog cares about: the tab changed."""
        if not lparam:
            return 0
        header = NMHDR.from_address(lparam)
        if header.code != TCN_SELCHANGE or header.hwndFrom != self._tab_handle:
            return 0
        self._show_tab(self._selected_tab_index())
        return 0

    def _selected_tab_index(self) -> int:
        """Ask the tab strip which page it has moved to."""
        return int(self._user32.SendMessageW(self._tab_handle, TCM_GETCURSEL, 0, 0))

    def _show_tab(self, index: int) -> None:
        """Show one page window and hide every other one."""
        if not 0 <= index < len(self._page_handles):
            return
        for page, handle in enumerate(self._page_handles):
            self._user32.ShowWindow(handle, SW_SHOW if page == index else SW_HIDE)

    def _section_picked(self, side: SideList) -> None:
        """Show the section the user picked in a side list."""
        picked = int(self._user32.SendMessageW(side.handle, LB_GETCURSEL, 0, 0))
        self._show_section(side, picked)

    def _show_section(self, side: SideList, index: int) -> None:
        """Show one section window and hide the others; ignore an index it lacks."""
        if not 0 <= index < len(side.sections):
            return
        for position, handle in enumerate(side.sections):
            self._user32.ShowWindow(handle, SW_SHOW if position == index else SW_HIDE)

    def _field_used(self, index: int, notification: int) -> None:
        """Record a checkbox click or a picked entry, or run a link or command button."""
        item = self._fields[index]
        if isinstance(item, SettingLink):
            self._open_link(item)
            return
        if isinstance(item, SettingCommand):
            self._run_command(item)
            return
        if isinstance(item, SettingToggle):
            self._pending.edit(item.key, self._reads_checked(self._controls[item.key].label))
            return
        if isinstance(item, SettingChoice) and notification == CBN_SELCHANGE:
            self._choice_picked(item)

    def _choice_picked(self, item) -> None:
        """Record the entry the user picked from a drop-down list."""
        controls = self._controls.get(item.key)
        if controls is None or controls.editor is None:
            return
        picked = int(self._user32.SendMessageW(controls.editor, CB_GETCURSEL, 0, 0))
        values = self._choice_values.get(item.key, [])
        if 0 <= picked < len(values):
            self._pending.edit(item.key, values[picked])

    def _editor_typed(self, index: int) -> None:
        """Record what a number or text box now says, ignoring our own writes to it.

        An edit control announces its very first text while CreateWindowExW is
        still running, before there is a handle to read it back through, so an
        unknown box is normal rather than an error.
        """
        if self._syncing:
            return
        item = self._fields[index]
        controls = self._controls.get(item.key)
        if controls is None or controls.editor is None:
            return
        typed_text = self._control_text(controls.editor)
        if isinstance(item, SettingText):
            self._pending.edit(item.key, typed_text)
            return
        if not isinstance(item, SettingNumber):
            return
        typed = parse_number(item, typed_text)
        if typed is None:
            # A box part-way through being typed into; the last usable value
            # stands rather than being replaced by a half-formed one.
            return
        self._pending.edit(item.key, typed)

    def _run_command(self, command) -> None:
        """Run one command button on the model and effort shown, applied or not.

        A failure is kept out of the window procedure, like a link's.
        """
        model = str(self._pending.value_of(command.model_key) or "")
        effort = str(self._pending.value_of(command.effort_key) or "")
        try:
            command.act(model, effort, self._announce_output_changed)
        except Exception:
            log.exception("settings command %r failed", command.label)

    def _announce_output_changed(self) -> None:
        """Ask the window's own thread to reread its output boxes; callable from any thread.

        A run can outlive the window, so a closed window is simply not told.
        """
        handle = self._handle
        if handle is None:
            return
        self._user32.PostMessageW(handle, _WM_OUTPUT_CHANGED, 0, 0)

    def _open_link(self, link) -> None:
        """Open one link, keeping a failure out of the window procedure."""
        try:
            link.open()
        except Exception:
            log.exception("settings link %r failed", link.label)

    def _refresh_controls(self) -> None:
        """Show what every setting now actually reads, after an Apply.

        A number the setting clamped has to become visible, or the box keeps
        claiming a value that was never accepted.
        """
        self._syncing = True
        try:
            for item in self._fields:
                if isinstance(item, SettingToggle):
                    self._set_checked(
                        self._controls[item.key].label,
                        bool(self._pending.value_of(item.key)),
                    )
                elif isinstance(item, SettingNumber):
                    self._set_number(item)
                elif isinstance(item, SettingText):
                    self._user32.SetWindowTextW(
                        self._controls[item.key].editor,
                        str(self._pending.value_of(item.key) or ""),
                    )
                elif isinstance(item, SettingChoice):
                    self._select_choice(item)
        finally:
            self._syncing = False

    def _set_number(self, item) -> None:
        """Put one setting's current value back into its box and its arrows."""
        controls = self._controls[item.key]
        value = int(self._pending.value_of(item.key) or 0)
        self._user32.SetWindowTextW(controls.editor, str(value))
        self._user32.SendMessageW(controls.spinner, UDM_SETPOS32, 0, value)

    def _set_checked(self, handle: int, checked: bool) -> None:
        """Tick or untick one checkbox."""
        self._user32.SendMessageW(
            handle, BM_SETCHECK, BST_CHECKED if checked else BST_UNCHECKED, 0
        )

    def _reads_checked(self, handle: int) -> bool:
        """Report whether Windows currently shows one box as ticked."""
        return self._user32.SendMessageW(handle, BM_GETCHECK, 0, 0) == BST_CHECKED

    def _control_text(self, handle: int) -> str:
        """Read one control's text back out of Windows."""
        length = int(self._user32.GetWindowTextLengthW(handle))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def _reads_available(self, item) -> bool:
        """Report whether a setting can be changed, defaulting to yes."""
        try:
            return bool(item.available())
        except Exception:
            log.exception("unable to read whether %r is available", item.label)
            return True

    # ----------------------------------------------------------------- painting

    def _paint_background(self, device_context: int) -> int:
        """Fill the frame's client area with the themed background brush."""
        return self._fill(device_context, self._handle, self._background_brush)

    def _fill(self, device_context: int, window: int, brush: int | None) -> int:
        """Fill one window's whole client area with one brush."""
        rect = self._client_rect(window)
        self._user32.FillRect(device_context, ctypes.byref(rect), brush)
        return 1

    def _color_dialog_control(self, device_context: int) -> int:
        """Give a control on the frame the dialog's own background."""
        return self._color(
            device_context,
            background_color_for_theme(uses_light_theme=self._uses_light_theme()),
            self._background_brush,
        )

    def _color_page_control(self, device_context: int) -> int:
        """Give a control on a page the page's own background."""
        return self._color(
            device_context,
            page_background_color_for_theme(uses_light_theme=self._uses_light_theme()),
            self._page_brush,
        )

    def _color(self, device_context: int, color: int, brush: int | None) -> int:
        """Set one control's colours and hand back the brush behind it."""
        self._gdi32.SetTextColor(device_context, self._foreground_color)
        self._gdi32.SetBkColor(device_context, color)
        # Zero means "no brush of ours"; Windows then paints the default.
        return brush or 0

    def _color_editor(self, device_context: int) -> int:
        """Give a number box the lighter surface a text field is drawn on."""
        return self._color(
            device_context,
            field_background_color_for_theme(uses_light_theme=self._uses_light_theme()),
            self._field_brush,
        )

    def _release_resources(self) -> None:
        """Give back the font and brushes this window created.

        The process is long-lived and the window can be opened repeatedly, so
        leaking a GDI object per visit is a real leak rather than a tidy-up.
        """
        if self._font is not None and not self._font_is_stock:
            self._delete_object(self._font)
        self._font = None
        if self._monospace_font:
            self._delete_object(self._monospace_font)
        self._monospace_font = None
        win32_icon.destroy_icons(self._user32, self._icons)
        self._icons = []
        self._output_groups = []
        for brush in (
            "_background_brush",
            "_page_brush",
            "_field_brush",
            "_tab_inactive_brush",
            "_border_brush",
        ):
            handle = getattr(self, brush)
            if handle is not None:
                self._delete_object(handle)
            setattr(self, brush, None)

    def _delete_object(self, handle: int) -> None:
        """Release one GDI object, reporting rather than raising on failure."""
        try:
            self._gdi32.DeleteObject(handle)
        except (AttributeError, OSError) as exc:
            log.warning("unable to release a settings window resource (%s)", exc)


def create_settings_window(model: SettingsModel) -> Win32SettingsWindow:
    """Build one settings window for the controller to show."""
    return Win32SettingsWindow(model)
