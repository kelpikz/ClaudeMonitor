"""Where every control in the settings dialog sits.

Pure geometry: a model, a way to measure text, how much of itself the tab
control wraps around a page, and the display's DPI go in; rectangles come out.
Nothing here calls user32 or gdi32, which is what lets the arithmetic be tested
without a desktop — the adapter next door in ``win32_settings_window`` does the
drawing and is the only caller that has to run on Windows.

Every number below is written for 96 DPI and scaled at layout time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import Insets, Rect
from .settings import (
    SettingChoice,
    SettingCommand,
    SettingLink,
    SettingNumber,
    SettingOutput,
    SettingText,
    SettingToggle,
    SettingsModel,
)
from .win32_bindings import USER_DEFAULT_SCREEN_DPI
from .win32_dpi import scale_for_dpi


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
_TEXT_WIDTH = 150  # A model name box.
_CHOICE_WIDTH = 110  # An effort list, closed.
_OUTPUT_HEIGHT = 112  # A read-only box: about seven lines of the dialog font.
_OUTPUT_MIN_WIDTH = 300
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
_LIST_MIN_WIDTH = 90  # The side list of a sectioned tab.
_LIST_TEXT_PADDING = 16  # Room either side of an entry, and for a scroll bar.

# Used only until the tab control can be asked how much of itself surrounds a
# page; a creation that fails outright leaves these standing.
_FALLBACK_TAB_FRAME = Insets(4, 25, 4, 4)
_MAX_SANE_TAB_INSET = 60


@dataclass(frozen=True)
class FieldLayout:
    """Where one field's controls sit, in client coordinates.

    ``rect`` is the checkbox, the link button, or the label of a boxed field. ``editor`` is the box of a number, a text, or a choice; the
    spinner and the unit exist only for a number.
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
class SectionLayout:
    """One section of a sectioned tab: a window beside the side list.

    ``rect`` is in the page's coordinates. The groups are in the section's
    own, because the section is a window of its own, as a page is.
    """

    title: str
    rect: Rect
    groups: list[GroupLayout] = field(default_factory=list)


@dataclass(frozen=True)
class TabLayout:
    """One page of the dialog. Every page occupies the same rectangle.

    A sectioned page has a ``side_list`` down its left side, an ``add_button``
    under it when the tab offers one, and one entry in ``sections`` per item.
    """

    title: str
    groups: list[GroupLayout] = field(default_factory=list)
    side_list: Rect | None = None
    add_button: Rect | None = None
    sections: list[SectionLayout] = field(default_factory=list)


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


def _is_button(item) -> bool:
    """Return whether a field is drawn as a push button."""
    return isinstance(item, (SettingLink, SettingCommand))


def _row_height(item, scaled: Callable[[int], int]) -> int:
    """Return the height one field's row needs: a button and an output box are taller."""
    if isinstance(item, SettingOutput):
        return scaled(_OUTPUT_HEIGHT)
    return scaled(_BUTTON_HEIGHT if _is_button(item) else _ROW_HEIGHT)


def _rows(items) -> list[list]:
    """Split one group's fields into rows: buttons side by side share one row.

    Copy command and Run now belong together, and two buttons stacked one per
    row read as two unrelated things.
    """
    rows: list[list] = []
    for item in items:
        if rows and _is_button(item) and _is_button(rows[-1][-1]):
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def _button_row_width(row, measure: Callable[[str], int], scaled) -> int:
    """Return the width a row of buttons needs, the gaps between them included."""
    widths = [_button_width(item.label, measure, scaled) for item in row]
    return sum(widths) + scaled(_BUTTON_GAP) * (len(widths) - 1)


def _group_content_width(group, measure: Callable[[str], int], scaled) -> int:
    """Return the width one group box needs inside the page.

    The window is sized around its own text rather than to a fixed width, so a
    longer setting name widens the dialog instead of being clipped by it. The
    numbers are measured from the shared column their boxes line up on, so a
    short label beside a long one does not report a width its unit overflows.
    """
    column = _number_column(group.fields, 0, measure, scaled)
    return max(
        (
            _button_row_width(row, measure, scaled)
            if _is_button(row[0])
            else _content_width(row[0], measure, scaled, column)
            for row in _rows(group.fields)
        ),
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
    if isinstance(item, SettingText):
        return column + scaled(_TEXT_WIDTH)
    if isinstance(item, SettingChoice):
        return column + scaled(_CHOICE_WIDTH)
    if isinstance(item, SettingOutput):
        return scaled(_OUTPUT_MIN_WIDTH)
    return _button_width(item.label, measure, scaled)


def _button_width(label: str, measure: Callable[[str], int], scaled) -> int:
    """Return the width a push button needs for one label."""
    return max(scaled(_BUTTON_MIN_WIDTH), measure(label) + scaled(_BUTTON_PADDING))


def _group_height(group, scaled: Callable[[int], int]) -> int:
    """Return the height one group box needs for its caption and its rows."""
    rows = _rows(group.fields)
    heights = sum(_row_height(row[0], scaled) for row in rows)
    gaps = scaled(_ROW_GAP) * max(0, len(rows) - 1)
    return scaled(_GROUP_CAPTION_HEIGHT) + heights + gaps + scaled(_GROUP_BOTTOM_PADDING)


def _stack_height(groups, scaled: Callable[[int], int]) -> int:
    """Return the height a set of group boxes needs, stacked with a gap between."""
    boxes = sum(_group_height(group, scaled) for group in groups)
    return boxes + scaled(_GROUP_GAP) * max(0, len(groups) - 1)


def _tab_height(tab, scaled: Callable[[int], int]) -> int:
    """Return the height one page needs: its own groups, or its tallest section."""
    if not tab.sections:
        return _stack_height(tab.groups, scaled)
    return max(_stack_height(section.groups, scaled) for section in tab.sections)


def _list_width(tab, measure: Callable[[str], int], scaled) -> int:
    """Return the width of a sectioned tab's side list: its longest entry, or its button."""
    entries = [measure(section.title) + scaled(_LIST_TEXT_PADDING) for section in tab.sections]
    if tab.add_section is not None:
        entries.append(_button_width(tab.add_section.label, measure, scaled))
    return max(scaled(_LIST_MIN_WIDTH), *entries)


def _tab_width(tab, measure: Callable[[str], int], scaled, padding: int) -> int:
    """Return the width one page needs, its padding and any side list included.

    ``padding`` is what sits either side of a group box's contents: the page's
    inset plus the box's own.
    """
    widest = max(
        (_group_content_width(group, measure, scaled) for group in tab.all_groups()),
        default=0,
    )
    if not tab.sections:
        return widest + 2 * padding
    # A section starts at the list's right edge and pads its groups like a page.
    return scaled(_PAGE_PADDING) + _list_width(tab, measure, scaled) + widest + 2 * padding


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

    widest_tab = max(
        (
            _tab_width(tab, measure, scaled, page_padding + group_padding)
            for tab in model.tabs
        ),
        default=0,
    )
    page_width = max(
        scaled(_MIN_PAGE_WIDTH),
        widest_tab,
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
    """Place one page: its own group boxes, or a side list and its sections."""
    if tab.sections:
        return _place_sectioned_tab(tab, page, measure, scaled, page_padding, group_padding)
    return TabLayout(
        title=tab.title,
        groups=_place_groups(tab.groups, page, measure, scaled, page_padding, group_padding),
    )


def _place_sectioned_tab(
    tab, page: Rect, measure, scaled, page_padding: int, group_padding: int
) -> TabLayout:
    """Place a side list down the left, a button under it, and every section beside it.

    Every section gets the same rectangle, because only one shows at a time.
    """
    left = page.left + page_padding
    right = left + _list_width(tab, measure, scaled)
    bottom = page.bottom - page_padding
    add_button = None
    if tab.add_section is not None:
        add_button = Rect(left, bottom - scaled(_BUTTON_HEIGHT), right, bottom)
        bottom = add_button.top - scaled(_ROW_GAP)
    section_rect = Rect(right, page.top, page.right, page.bottom)
    section_client = Rect(0, 0, section_rect.width, section_rect.height)
    sections = [
        SectionLayout(
            title=section.title,
            rect=section_rect,
            groups=_place_groups(
                section.groups, section_client, measure, scaled, page_padding, group_padding
            ),
        )
        for section in tab.sections
    ]
    return TabLayout(
        title=tab.title,
        side_list=Rect(left, page.top + page_padding, right, bottom),
        add_button=add_button,
        sections=sections,
    )


def _place_groups(
    groups, area: Rect, measure, scaled, page_padding: int, group_padding: int
) -> list[GroupLayout]:
    """Stack group boxes from the top of one area down, inset on every edge."""
    placed: list[GroupLayout] = []
    top = area.top + page_padding
    for group in groups:
        height = _group_height(group, scaled)
        rect = Rect(area.left + page_padding, top, area.right - page_padding, top + height)
        placed.append(
            GroupLayout(
                title=group.title,
                rect=rect,
                caption=_place_caption(group.title, rect, measure, scaled),
                fields=_place_fields(group.fields, rect, measure, scaled, group_padding),
            )
        )
        top = rect.bottom + scaled(_GROUP_GAP)
    return placed


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
    """Stack one group's rows under its caption, buttons in a row side by side."""
    placed: list[FieldLayout] = []
    left = box.left + group_padding
    right = box.right - group_padding
    top = box.top + scaled(_GROUP_CAPTION_HEIGHT)
    column = _number_column(items, left, measure, scaled)
    for row in _rows(items):
        height = _row_height(row[0], scaled)
        x = left
        for item in row:
            field_layout = _place_field(item, x, right, top, height, measure, scaled, column)
            placed.append(field_layout)
            x = field_layout.rect.right + scaled(_BUTTON_GAP)
        top += height + scaled(_ROW_GAP)
    return placed


def _number_column(items, left: int, measure: Callable[[str], int], scaled) -> int:
    """Return the x every box in one group starts at: number, text, or choice.

    Boxes under one caption read as a set, so they line up with each other
    rather than each sitting wherever its own label happens to end.
    """
    labels = [
        measure(item.label)
        for item in items
        if isinstance(item, (SettingNumber, SettingText, SettingChoice))
    ]
    return left + max(labels, default=0) + scaled(_LABEL_GAP)


def _place_field(item, left: int, right: int, top: int, height: int, measure, scaled, column: int):
    """Place one field's controls on the row it has been given."""
    if isinstance(item, SettingOutput):
        return FieldLayout(key=item.key, rect=Rect(left, top, right, top + height))
    if _is_button(item):
        width = _button_width(item.label, measure, scaled)
        return FieldLayout(key=item.key, rect=Rect(left, top, left + width, top + height))
    if isinstance(item, SettingToggle):
        return FieldLayout(key=item.key, rect=Rect(left, top, right, top + height))
    if isinstance(item, (SettingText, SettingChoice)):
        width = _TEXT_WIDTH if isinstance(item, SettingText) else _CHOICE_WIDTH
        return FieldLayout(
            key=item.key,
            rect=Rect(left, top, left + measure(item.label), top + height),
            editor=Rect(column, top, column + scaled(width), top + height),
        )

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
