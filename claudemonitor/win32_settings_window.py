"""The settings dialog, drawn with Windows' own controls.

This is the second module allowed to call user32 and gdi32 directly; like
``win32_taskbar_window`` it holds only the drawing, and the decisions about
what the window contains live next door in ``settings.py``.

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
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Insets, Rect
from .settings import (
    PendingSettings,
    SettingLink,
    SettingNumber,
    SettingToggle,
    SettingsModel,
    WINDOW_TITLE,
    parse_number,
)
from . import win32_text
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
    COMCTL32_SIGNATURES,
    CS_HREDRAW,
    CS_VREDRAW,
    CW_USEDEFAULT,
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
    EN_CHANGE,
    ES_AUTOHSCROLL,
    ES_LEFT,
    ES_NUMBER,
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
    WM_CLOSE,
    WM_COMMAND,
    WM_CTLCOLORBTN,
    WM_CTLCOLOREDIT,
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
    apply_signatures,
    background_color_for_theme,
    field_background_color_for_theme,
    foreground_color_for_theme,
    page_background_color_for_theme,
    system_uses_light_theme,
)
from .win32_taskbar_window import scale_for_dpi

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
_UNUSED_ID = 0  # Group boxes and labels are never clicked, so never routed.

# Everything below is written for 96 DPI and scaled at layout time.
_MARGIN = 12  # Between the window edge and the tab control.
_PAGE_PADDING = 10  # Between the page edge and a group box.
_GROUP_GAP = 10  # Between two group boxes.
_GROUP_CAPTION_HEIGHT = 20  # From a group's top to its first row.
_GROUP_CAPTION_LEFT = 9  # Where Windows starts a group box's own caption.
_GROUP_CAPTION_TOP = 2
_GROUP_CAPTION_TEXT_HEIGHT = 15
_GROUP_CAPTION_PADDING = 4  # Enough either side to cover the border behind.
_GROUP_PADDING = 12  # Inside a group box, either side.
_GROUP_BOTTOM_PADDING = 12
_ROW_HEIGHT = 22
_ROW_GAP = 6
_CHECKBOX_INDICATOR = 20  # The box itself, drawn before the label.
_NUMBER_WIDTH = 64  # A number box, spin arrows included.
_LABEL_GAP = 8
_SPINNER_WIDTH = 17  # What Windows draws the arrows at, at 96 DPI.
_BUTTON_HEIGHT = 26
_BUTTON_MIN_WIDTH = 84
_BUTTON_PADDING = 20  # Breathing room either side of a button's own text.
_BUTTON_GAP = 8
_BUTTON_ROW_GAP = 12  # Between the tab control and the row below it.
_TAB_CAPTION_PADDING = 26  # What one tab adds around its own title.
_MIN_PAGE_WIDTH = 320
_TAB_JOIN_HEIGHT = 2  # How far the current tab reaches over the page's outline.

# Used only until the tab control can be asked how much of itself surrounds a
# page; a creation that fails outright leaves these standing.
_FALLBACK_TAB_FRAME = Insets(4, 25, 4, 4)
_MAX_SANE_TAB_INSET = 60

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


@dataclass(frozen=True)
class FieldLayout:
    """Where one field's controls sit, in client coordinates.

    ``rect`` is the checkbox, the link button, or a number's own label. The
    three optional rectangles exist only for a number.
    """

    key: str
    rect: Rect
    editor: Rect | None = None
    spinner: Rect | None = None
    suffix: Rect | None = None


@dataclass(frozen=True)
class GroupLayout:
    """One captioned box and everything inside it.

    The caption is placed separately because it is drawn separately: a group
    box paints its own title in a colour the theme chooses, which in dark mode
    is black on a dark page.
    """

    title: str
    rect: Rect
    caption: Rect
    fields: list[FieldLayout] = field(default_factory=list)


@dataclass(frozen=True)
class TabLayout:
    """One page of the dialog. Every page occupies the same rectangle."""

    title: str
    groups: list[GroupLayout] = field(default_factory=list)


@dataclass(frozen=True)
class SettingsLayout:
    """Where every control sits, in client coordinates."""

    width: int
    height: int
    tab: Rect
    page: Rect
    tabs: list[TabLayout]
    ok: Rect
    cancel: Rect
    apply: Rect


def _row_height(item, scaled: Callable[[int], int]) -> int:
    """Return the height one field's row needs; only a button is taller."""
    return scaled(_BUTTON_HEIGHT if isinstance(item, SettingLink) else _ROW_HEIGHT)


def _group_content_width(group, measure: Callable[[str], int], scaled) -> int:
    """Return the width one group box needs inside the page.

    The window is sized around its own text rather than to a fixed width, so a
    longer setting name widens the dialog instead of being clipped by it. The
    numbers are measured from the shared column their boxes line up on, so a
    short label beside a long one does not report a width its unit overflows.
    """
    column = _number_column(group.fields, 0, measure, scaled)
    return max(
        (_content_width(item, measure, scaled, column) for item in group.fields),
        default=0,
    )


def _content_width(item, measure: Callable[[str], int], scaled, column: int) -> int:
    """Return the width one field needs inside its group box."""
    if isinstance(item, SettingToggle):
        return scaled(_CHECKBOX_INDICATOR) + measure(item.label)
    if isinstance(item, SettingNumber):
        return (
            column
            + scaled(_NUMBER_WIDTH + _LABEL_GAP)
            + measure(item.suffix)
        )
    return _button_width(item.label, measure, scaled)


def _button_width(label: str, measure: Callable[[str], int], scaled) -> int:
    """Return the width a push button needs for one label."""
    return max(scaled(_BUTTON_MIN_WIDTH), measure(label) + scaled(_BUTTON_PADDING))


def _group_height(group, scaled: Callable[[int], int]) -> int:
    """Return the height one group box needs for its caption and its rows."""
    rows = sum(_row_height(item, scaled) for item in group.fields)
    gaps = scaled(_ROW_GAP) * max(0, len(group.fields) - 1)
    return scaled(_GROUP_CAPTION_HEIGHT) + rows + gaps + scaled(_GROUP_BOTTOM_PADDING)


def _tab_height(tab, scaled: Callable[[int], int]) -> int:
    """Return the height one page needs for every group stacked down it."""
    boxes = sum(_group_height(group, scaled) for group in tab.groups)
    return boxes + scaled(_GROUP_GAP) * max(0, len(tab.groups) - 1)


def _tab_strip_width(model, measure: Callable[[str], int], scaled) -> int:
    """Return the width every tab caption needs side by side."""
    return sum(measure(tab.title) + scaled(_TAB_CAPTION_PADDING) for tab in model.tabs)


def settings_layout(
    model: SettingsModel,
    *,
    measure: Callable[[str], int],
    tab_frame: Insets,
    dpi: int = USER_DEFAULT_SCREEN_DPI,
) -> SettingsLayout:
    """Place the tab control, every page's controls, and the button row.

    ``tab_frame`` is how much of itself the tab control wraps around the page,
    which only the control itself can answer; passing it in keeps this a pure
    function of the model and its measured text.
    """
    scaled = lambda value: scale_for_dpi(value, dpi)  # noqa: E731 - a local alias
    margin = scaled(_MARGIN)
    page_padding = scaled(_PAGE_PADDING)
    group_padding = scaled(_GROUP_PADDING)

    buttons = [_button_width(label, measure, scaled) for label in ("OK", "Cancel", "Apply")]
    button_row_width = sum(buttons) + scaled(_BUTTON_GAP) * (len(buttons) - 1)

    widest_field = max(
        (
            _group_content_width(group, measure, scaled)
            for tab in model.tabs
            for group in tab.groups
        ),
        default=0,
    )
    page_width = max(
        scaled(_MIN_PAGE_WIDTH),
        widest_field + 2 * (page_padding + group_padding),
        _tab_strip_width(model, measure, scaled),
        button_row_width - tab_frame.left - tab_frame.right,
    )
    # The groups are inset from the page on every edge, so the tallest page
    # needs that padding above the first box and below the last one too.
    page_height = 2 * page_padding + max(
        (_tab_height(tab, scaled) for tab in model.tabs), default=0
    )

    tab = Rect(
        margin,
        margin,
        margin + tab_frame.left + page_width + tab_frame.right,
        margin + tab_frame.top + page_height + tab_frame.bottom,
    )
    page = Rect(
        tab.left + tab_frame.left,
        tab.top + tab_frame.top,
        tab.right - tab_frame.right,
        tab.bottom - tab_frame.bottom,
    )

    width = tab.right + margin
    # Each page is a window of its own, so its contents are placed from its
    # own top left corner rather than from the frame's.
    page_client = Rect(0, 0, page.width, page.height)
    tabs = [
        _place_tab(tab_model, page_client, measure, scaled, page_padding, group_padding)
        for tab_model in model.tabs
    ]
    ok, cancel, apply_button = _place_buttons(
        buttons, right=width - margin, top=tab.bottom + scaled(_BUTTON_ROW_GAP), scaled=scaled
    )
    return SettingsLayout(
        width=width,
        height=apply_button.bottom + margin,
        tab=tab,
        page=page,
        tabs=tabs,
        ok=ok,
        cancel=cancel,
        apply=apply_button,
    )


def _place_buttons(
    widths: list[int], *, right: int, top: int, scaled: Callable[[int], int]
) -> tuple[Rect, Rect, Rect]:
    """Lay OK, Cancel, and Apply out in a row that ends at the right margin."""
    gap = scaled(_BUTTON_GAP)
    bottom = top + scaled(_BUTTON_HEIGHT)
    placed: list[Rect] = []
    edge = right
    for button_width in reversed(widths):
        placed.insert(0, Rect(edge - button_width, top, edge, bottom))
        edge -= button_width + gap
    return placed[0], placed[1], placed[2]


def _place_tab(
    tab, page: Rect, measure, scaled, page_padding: int, group_padding: int
) -> TabLayout:
    """Stack one page's group boxes from the top of the page down."""
    groups: list[GroupLayout] = []
    top = page.top + page_padding
    for group in tab.groups:
        height = _group_height(group, scaled)
        rect = Rect(page.left + page_padding, top, page.right - page_padding, top + height)
        groups.append(
            GroupLayout(
                title=group.title,
                rect=rect,
                caption=_place_caption(group.title, rect, measure, scaled),
                fields=_place_fields(group.fields, rect, measure, scaled, group_padding),
            )
        )
        top = rect.bottom + scaled(_GROUP_GAP)
    return TabLayout(title=tab.title, groups=groups)


def _place_caption(title: str, box: Rect, measure, scaled) -> Rect:
    """Place one group's caption over the top left of its own border."""
    left = box.left + scaled(_GROUP_CAPTION_LEFT)
    top = box.top + scaled(_GROUP_CAPTION_TOP)
    return Rect(
        left,
        top,
        left + measure(title) + scaled(_GROUP_CAPTION_PADDING),
        top + scaled(_GROUP_CAPTION_TEXT_HEIGHT),
    )


def _place_fields(items, box: Rect, measure, scaled, group_padding: int) -> list[FieldLayout]:
    """Stack one group's fields under its caption."""
    placed: list[FieldLayout] = []
    left = box.left + group_padding
    right = box.right - group_padding
    top = box.top + scaled(_GROUP_CAPTION_HEIGHT)
    column = _number_column(items, left, measure, scaled)
    for item in items:
        height = _row_height(item, scaled)
        placed.append(_place_field(item, left, right, top, height, measure, scaled, column))
        top += height + scaled(_ROW_GAP)
    return placed


def _number_column(items, left: int, measure: Callable[[str], int], scaled) -> int:
    """Return the x every number box in one group starts at.

    Two numbers under one caption read as a pair, so their boxes line up with
    each other rather than each sitting wherever its own label happens to end.
    """
    labels = [
        measure(item.label) for item in items if isinstance(item, SettingNumber)
    ]
    return left + max(labels, default=0) + scaled(_LABEL_GAP)


def _place_field(item, left: int, right: int, top: int, height: int, measure, scaled, column: int):
    """Place one field's controls on the row it has been given."""
    if isinstance(item, SettingLink):
        width = _button_width(item.label, measure, scaled)
        return FieldLayout(key=item.key, rect=Rect(left, top, left + width, top + height))
    if isinstance(item, SettingToggle):
        return FieldLayout(key=item.key, rect=Rect(left, top, right, top + height))

    label_right = left + measure(item.label)
    editor_left = column
    editor = Rect(editor_left, top, editor_left + scaled(_NUMBER_WIDTH), top + height)
    spinner_width = scaled(_SPINNER_WIDTH)
    return FieldLayout(
        key=item.key,
        rect=Rect(left, top, label_right, top + height),
        editor=editor,
        # The arrows sit inside the right edge of the box they serve, which is
        # what UDS_ALIGNRIGHT makes Windows draw anyway; giving them the same
        # rectangle keeps the two agreeing before Windows adjusts them.
        spinner=Rect(editor.right - spinner_width, top, editor.right, top + height),
        suffix=Rect(
            editor.right + scaled(_LABEL_GAP),
            top,
            editor.right + scaled(_LABEL_GAP) + measure(item.suffix),
            top + height,
        ),
    )


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
        self._ok_handle: int | None = None
        self._font: int | None = None
        self._font_is_stock = False
        self._background_brush: int | None = None
        self._page_brush: int | None = None
        self._field_brush: int | None = None
        self._tab_inactive_brush: int | None = None
        self._border_brush: int | None = None
        self._tab_frame = _FALLBACK_TAB_FRAME
        self._original_tab_proc: int | None = None
        self._foreground_color = 0
        self._field_handles: dict[str, int] = {}
        self._editor_handles: dict[str, int] = {}
        self._spinner_handles: dict[str, int] = {}
        self._current_tab = 0
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
            for handle in [*self._page_handles, self._handle]:
                if handle is not None:
                    _active_windows.pop(handle, None)
            self._page_handles = []
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
        self._handle = self._create_frame()
        _active_windows[self._handle] = self
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
            self._create_page(tab, layout.page, indexed) for tab in layout.tabs
        ]

    def _create_page(self, tab: TabLayout, page: Rect, indexed: dict[str, int]) -> int:
        """Create one page window over the tab's display area, and its contents.

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
            page.left,
            page.top,
            page.width,
            page.height,
            self._handle,
            _UNUSED_ID,
            self._kernel32.GetModuleHandleW(None),
            None,
        )
        _active_windows[handle] = self
        self._apply_dark_control_theme(handle)
        for group in tab.groups:
            # The fields come first: a group box fills its own interior with
            # the colour it is given, and a child made later sits lower in the
            # z-order, which is what leaves the box behind what it surrounds.
            for placed in group.fields:
                self._create_field(self._fields[indexed[placed.key]], placed, handle)
            self._create_group_box(group, handle)
        return handle

    def _create_group_box(self, group: GroupLayout, parent: int) -> int:
        """Create one box around a set of related fields, and caption it.

        The caption is a static of our own rather than the group box's title,
        because a group box paints its title in whichever colour the visual
        style picks — which on a dark page is black on near-black. The static
        is created first, so it sits above the border it interrupts.
        """
        self._create_control(
            STATIC_CLASS, group.title, SS_LEFT, group.caption, _UNUSED_ID, parent
        )
        return self._create_control(
            BUTTON_CLASS,
            "",
            BS_GROUPBOX | WS_GROUP,
            group.rect,
            _UNUSED_ID,
            parent,
        )

    def _create_field(self, item, placed: FieldLayout, parent: int) -> list[int]:
        """Create the controls one field needs, and remember how to reach them."""
        index = self._fields.index(item)
        if isinstance(item, SettingToggle):
            return [self._create_checkbox(item, placed, index, parent)]
        if isinstance(item, SettingNumber):
            return self._create_number(item, placed, index, parent)
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
        self._field_handles[item.key] = handle
        self._set_checked(handle, bool(self._pending.value_of(item.key)))
        if not self._reads_available(item):
            self._user32.EnableWindow(handle, False)
        return handle

    def _create_number(self, item, placed: FieldLayout, index: int, parent: int) -> list[int]:
        """Create a number's label, its box, its spin arrows, and its unit."""
        label = self._create_control(
            STATIC_CLASS, item.label, SS_LEFT, placed.rect, _UNUSED_ID, parent
        )
        self._field_handles[item.key] = label
        editor = self._create_control(
            EDIT_CLASS,
            str(self._pending.value_of(item.key)),
            ES_LEFT | ES_NUMBER | ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP,
            placed.editor,
            _FIRST_EDITOR_ID + index,
            parent,
        )
        self._editor_handles[item.key] = editor
        spinner = self._create_spinner(item, placed, index, editor, parent)
        suffix = self._create_control(
            STATIC_CLASS, item.suffix, SS_LEFT, placed.suffix, _UNUSED_ID, parent
        )
        return [label, editor, spinner, suffix]

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
        self._spinner_handles[item.key] = handle
        self._user32.SendMessageW(handle, UDM_SETBUDDY, editor, 0)
        self._user32.SendMessageW(handle, UDM_SETRANGE32, item.minimum, item.maximum)
        self._user32.SendMessageW(
            handle, UDM_SETPOS32, 0, int(self._pending.value_of(item.key) or 0)
        )
        return handle

    def _create_dialog_buttons(self, layout: SettingsLayout) -> None:
        """Create OK, Cancel, and Apply on the frame below the tab control."""
        self._ok_handle = self._create_control(
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
        try:
            self._uxtheme.SetWindowTheme(handle, DARK_MODE_CONTROL_THEME, None)
        except (AttributeError, OSError) as exc:
            log.warning("unable to apply the dark control theme (%s)", exc)

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
        if message == WM_COMMAND:
            return self._on_command(hwnd, wparam & 0xFFFF, (wparam >> 16) & 0xFFFF)
        if message == WM_NOTIFY:
            return self._on_notify(lparam)
        if message == WM_ERASEBKGND:
            return self._paint_background(wparam)
        if message == WM_CTLCOLOREDIT:
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
        if message == WM_CTLCOLOREDIT:
            return self._color_editor(wparam)
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
            return self._color_page_control(wparam)
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _on_command(self, hwnd: int, control_id: int, notification: int) -> int:
        """Route a click, a typed number, or a dialog button to what owns it."""
        field_index = control_id - _FIRST_FIELD_ID
        if 0 <= field_index < len(self._fields):
            self._field_used(field_index)
            return 0

        editor_index = control_id - _FIRST_EDITOR_ID
        if notification == EN_CHANGE and 0 <= editor_index < len(self._fields):
            self._number_typed(editor_index)
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
        self._current_tab = index
        for page, handle in enumerate(self._page_handles):
            self._user32.ShowWindow(handle, SW_SHOW if page == index else SW_HIDE)

    def _field_used(self, index: int) -> None:
        """Record a checkbox click, or run a link button."""
        item = self._fields[index]
        if isinstance(item, SettingLink):
            self._open_link(item)
            return
        if isinstance(item, SettingToggle):
            self._pending.edit(item.key, self._reads_checked(self._field_handles[item.key]))

    def _number_typed(self, index: int) -> None:
        """Record what a number box now says, ignoring our own writes to it.

        An edit control announces its very first text while CreateWindowExW is
        still running, before there is a handle to read it back through, so an
        unknown box is normal rather than an error.
        """
        if self._syncing:
            return
        item = self._fields[index]
        if not isinstance(item, SettingNumber) or item.key not in self._editor_handles:
            return
        typed = parse_number(item, self._control_text(self._editor_handles[item.key]))
        if typed is None:
            # A box part-way through being typed into; the last usable value
            # stands rather than being replaced by a half-formed one.
            return
        self._pending.edit(item.key, typed)

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
                        self._field_handles[item.key], bool(self._pending.value_of(item.key))
                    )
                elif isinstance(item, SettingNumber):
                    self._set_number(item)
        finally:
            self._syncing = False

    def _set_number(self, item) -> None:
        """Put one setting's current value back into its box and its arrows."""
        value = int(self._pending.value_of(item.key) or 0)
        self._user32.SetWindowTextW(self._editor_handles[item.key], str(value))
        self._user32.SendMessageW(self._spinner_handles[item.key], UDM_SETPOS32, 0, value)

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
