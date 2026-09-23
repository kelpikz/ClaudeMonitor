"""Tests for the native settings dialog.

The geometry is a pure function and is tested as one. The window itself is
driven through the same fake DLLs the taskbar label uses: every Windows call is
recorded, so a click can be delivered to the real window procedure and the
resulting native calls asserted on without a window ever existing.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import replace
from pathlib import Path

from claudemonitor.models import CLAUDE, CODEX
from claudemonitor.settings import (
    ProviderFields,
    SettingLink,
    SettingNumber,
    SettingToggle,
    SettingsGroup,
    SettingsModel,
    SettingsTab,
    build_settings,
)
from claudemonitor.win32_bindings import (
    GWLP_WNDPROC,
    TCM_GETITEMCOUNT,
    TCM_GETITEMRECT,
    BM_SETCHECK,
    BST_CHECKED,
    BST_UNCHECKED,
    BS_AUTOCHECKBOX,
    BS_GROUPBOX,
    BUTTON_CLASS,
    DARK_THEME_PAGE_BACKGROUND,
    EDIT_CLASS,
    EN_CHANGE,
    HWND_BOTTOM,
    IDCANCEL,
    IDOK,
    LIGHT_THEME_BACKGROUND,
    LIGHT_THEME_PAGE_BACKGROUND,
    LIGHT_THEME_FIELD_BACKGROUND,
    NMHDR,
    STATIC_CLASS,
    SW_HIDE,
    SW_SHOW,
    TAB_CONTROL_CLASS,
    TCM_INSERTITEMW,
    TCN_SELCHANGE,
    UPDOWN_CLASS,
    WM_CLOSE,
    WM_COMMAND,
    WM_CTLCOLOREDIT,
    WM_CTLCOLORSTATIC,
    WM_ERASEBKGND,
    WM_NOTIFY,
    WM_SETFONT,
)
from claudemonitor.win32_settings_window import (
    _APPLY_ID,
    _FIRST_EDITOR_ID,
    _FIRST_FIELD_ID,
    Win32SettingsWindow,
)


def _brushes_filled(window) -> list[int]:
    """Return the brush every FillRect was made with, in order."""
    return [call[3] for call in window._user32.named("FillRect")]


def _brushes_framed(window) -> list[int]:
    """Return the brush every FrameRect was made with, in order."""
    return [call[3] for call in window._user32.named("FrameRect")]


# ------------------------------------------------------------------- fixtures


def _switch(on: bool = True, available: bool = True):
    """Build a switch over a mutable flag, plus the flag itself to assert on."""
    state = {"on": on}
    switch = SettingToggle(
        is_on=lambda: state["on"],
        write=lambda value: state.update(on=value),
        available=lambda: available,
    )
    return switch, state


def _number(value: int = 60, minimum: int = 10, maximum: int = 600):
    """Build a numeric setting over a mutable value, plus the value to assert on."""
    state = {"value": value}
    number = SettingNumber(
        value=lambda: state["value"],
        write=lambda new: state.update(value=new),
        minimum=minimum,
        maximum=maximum,
    )
    return number, state


def _production_model(**overrides) -> SettingsModel:
    """Build the real three-tab model, so the tests bind to what ships."""
    fields = {
        "taskbar": _switch()[0],
        "providers": [
            ProviderFields(provider=CLAUDE),
            ProviderFields(provider=CODEX, tracking=_switch()[0]),
        ],
        "session_refresh": _switch()[0],
        "startup": _switch()[0],
        "poll_interval": _number()[0],
        "amber_threshold": _number(50, 1, 99)[0],
        "red_threshold": _number(20, 1, 99)[0],
        "refresh_cooldown": _number(900, 60, 86400)[0],
        "log_dir": Path("."),
        "open_url": lambda url: None,
        "open_folder": lambda path: None,
    }
    fields.update(overrides)
    return build_settings(**fields)


def _toggle_field(key="taskbar", label="Show usage in the taskbar", **kwargs):
    """One checkbox field, over a flag the caller can inspect."""
    switch, state = _switch(**kwargs)
    return replace(switch, key=key, label=label), state


def _number_field(key="poll_interval", label="Check usage every", **kwargs):
    """One number field, over a value the caller can inspect."""
    number, state = _number(**kwargs)
    return replace(number, key=key, label=label, suffix="seconds"), state


def _one_tab(*fields, title: str = "General", group: str = "Startup") -> SettingsModel:
    """Wrap the given fields in a single tab holding a single group."""
    return SettingsModel(
        tabs=[SettingsTab(title=title, groups=[SettingsGroup(title=group, fields=list(fields))])]
    )


def _measure(text: str) -> int:
    """Stand in for GDI text measurement: a fixed width per character."""
    return 7 * len(text)


# ------------------------------------------------------------------ fake DLLs


class _FakeDll:
    """Record every native call, returning a benign success value by default."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.results: dict[str, object] = {}

    def __getattr__(self, name: str):
        def call(*args):
            self.calls.append((name, *args))
            result = self.results.get(name, 1)
            return result(*args) if callable(result) else result

        return call

    def named(self, name: str) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] == name]

    def was_called(self, name: str) -> bool:
        return bool(self.named(name))


class _FakeUser32(_FakeDll):
    """Hand out a fresh handle per created window, as Windows would.

    It also remembers each control's text and check state, because the window
    reads both back: a number is only ever known by what its box says.
    """

    def __init__(self) -> None:
        super().__init__()
        self._next_handle = 100
        self.messages: list[int] = []
        self.message_result: int | None = None
        self.text: dict[int, str] = {}
        self.checked: dict[int, int] = {}
        self.enabled: dict[int, bool] = {}
        self.shown: dict[int, int] = {}
        self.classes: dict[int, str] = {}

    def CreateWindowExW(self, style, class_name, text, *rest):
        self.calls.append(("CreateWindowExW", style, class_name, text, *rest))
        self._next_handle += 1
        self.text[self._next_handle] = text or ""
        self.classes[self._next_handle] = class_name
        return self._next_handle

    def SendMessageW(self, handle, message, wparam, lparam):
        self.calls.append(("SendMessageW", handle, message, wparam, lparam))
        if message == BM_SETCHECK:
            self.checked[handle] = wparam
        answer = self.results.get("SendMessageW")
        if answer is not None:
            return answer(handle, message, wparam, lparam)
        return self.checked.get(handle, 0) if message == 0x00F0 else 1

    def SetWindowTextW(self, handle, text):
        self.calls.append(("SetWindowTextW", handle, text))
        self.text[handle] = text
        return 1

    def GetWindowTextLengthW(self, handle):
        self.calls.append(("GetWindowTextLengthW", handle))
        return len(self.text.get(handle, ""))

    def GetWindowTextW(self, handle, buffer, size):
        self.calls.append(("GetWindowTextW", handle, size))
        buffer.value = self.text.get(handle, "")
        return len(buffer.value)

    def EnableWindow(self, handle, enabled):
        self.calls.append(("EnableWindow", handle, enabled))
        self.enabled[handle] = bool(enabled)
        return 1

    def ShowWindow(self, handle, command):
        self.calls.append(("ShowWindow", handle, command))
        self.shown[handle] = command
        return 1

    def GetMessageW(self, message_pointer, handle, first, last):
        self.calls.append(("GetMessageW",))
        if self.message_result is not None:
            return self.message_result
        if not self.messages:
            return 0  # WM_QUIT: the loop ends.
        message_pointer._obj.message = self.messages.pop(0)
        return 1

    def handles_of_class(self, class_name: str) -> list[int]:
        return [
            handle
            for handle, name in self.classes.items()
            if name == class_name
        ]


def _window(model: SettingsModel | None = None, *, light: bool = True):
    """Build a settings window whose DLLs are replaced by recording fakes."""
    window = Win32SettingsWindow(
        model if model is not None else _production_model(),
        uses_light_theme=lambda: light,
    )
    window._user32 = _FakeUser32()
    window._gdi32 = _FakeDll()
    window._uxtheme = _FakeDll()
    window._dwmapi = _FakeDll()
    window._kernel32 = _FakeDll()
    window._comctl32 = _FakeDll()
    return window


def _created(window, class_name: str) -> list[tuple[object, ...]]:
    """Return the CreateWindowExW calls that made a control of the given class."""
    return [
        call
        for call in window._user32.named("CreateWindowExW")
        if call[2] == class_name
    ]


def _created_with_style(window, class_name: str, style: int) -> list[tuple[object, ...]]:
    """Return the created controls of one class carrying one style bit."""
    return [call for call in _created(window, class_name) if call[4] & 0x0F == style]


class _AnyRect:
    """Matches whatever RECT pointer a fill was handed."""

    def __eq__(self, other) -> bool:
        return True


ANY_RECT = _AnyRect()


def _fills(window) -> list[tuple[object, ...]]:
    """Return the FillRect calls, with the RECT pointer made comparable."""
    return [
        (name, device_context, ANY_RECT, brush)
        for name, device_context, _rect, brush in window._user32.named("FillRect")
    ]


def _command(window, control_id: int, notification: int = 0) -> None:
    """Deliver one WM_COMMAND, as Windows does when a control is used."""
    window._window_proc(
        window._handle, WM_COMMAND, (notification << 16) | control_id, 0
    )


def _switch_to_tab(window, index: int) -> None:
    """Deliver the notification the tab strip sends when the page changes."""
    window._selected_tab_index = lambda: index
    header = NMHDR()
    header.hwndFrom = window._tab_handle
    header.code = TCN_SELCHANGE
    window._window_proc(window._handle, WM_NOTIFY, 0, ctypes.addressof(header))


# --------------------------------------------------------------------- window


class TestWhatTheWindowCreates:
    """The dialog is a tab strip, a page of grouped controls, and three buttons."""

    def test_a_tab_control_is_created(self):
        window = _window()
        window._create()

        assert len(_created(window, TAB_CONTROL_CLASS)) == 1

    def test_one_tab_is_inserted_per_page(self):
        window = _window()
        window._create()

        inserts = [
            call
            for call in window._user32.named("SendMessageW")
            if call[2] == TCM_INSERTITEMW
        ]
        assert len(inserts) == 3

    def test_one_group_box_is_created_per_group(self):
        window = _window()
        window._create()

        model = window._model
        expected = sum(len(tab.groups) for tab in model.tabs)
        assert len(_created_with_style(window, BUTTON_CLASS, BS_GROUPBOX)) == expected

    def test_one_checkbox_is_created_per_switch(self):
        window = _window()
        window._create()

        checkboxes = _created_with_style(window, BUTTON_CLASS, BS_AUTOCHECKBOX)
        assert len(checkboxes) == 4

    def test_one_box_and_one_spinner_are_created_per_number(self):
        window = _window()
        window._create()

        assert len(_created(window, EDIT_CLASS)) == 4
        assert len(_created(window, UPDOWN_CLASS)) == 4

    def test_a_number_box_opens_showing_the_stored_value(self):
        field, _state = _number_field(value=45)
        window = _window(_one_tab(field))
        window._create()

        assert [call[3] for call in _created(window, EDIT_CLASS)] == ["45"]

    def test_one_page_window_is_created_per_tab(self):
        window = _window()
        window._create()

        assert len(window._page_handles) == 3

    def test_every_page_control_belongs_to_its_own_page(self):
        # A control laid straight onto the frame cannot be repainted once it
        # is hidden, which leaves the last page printed through the new one.
        window = _window()
        window._create()
        parents = {
            call[9]
            for call in window._user32.named("CreateWindowExW")
            if call[2] in (BUTTON_CLASS, EDIT_CLASS, UPDOWN_CLASS, STATIC_CLASS)
        }

        assert set(window._page_handles) <= parents

    def test_the_dialog_buttons_belong_to_the_frame(self):
        window = _window()
        window._create()
        buttons = [
            call
            for call in _created(window, BUTTON_CLASS)
            if call[3] in ("OK", "Cancel", "Apply")
        ]

        assert [call[9] for call in buttons] == [window._handle] * 3

    def test_a_group_box_is_created_after_everything_it_surrounds(self):
        # A group box draws a border through the caption and the fields sit
        # inside it, so it has to be the lowest of the three in the z-order —
        # which, for siblings, means the last one created.
        field, _state = _toggle_field()
        window = _window(_one_tab(field))
        window._create()
        created = [
            call[3]
            for call in window._user32.named("CreateWindowExW")
            if call[2] in (BUTTON_CLASS, STATIC_CLASS)
        ]

        assert created.index("Show usage in the taskbar") < created.index("")
        assert created.index("Startup") < created.index("")

    def test_a_group_is_captioned_by_a_static_rather_than_by_the_box(self):
        # A group box paints its own title in whichever colour the visual
        # style picks, which on a dark page is black on near-black.
        field, _state = _toggle_field()
        window = _window(_one_tab(field))
        window._create()

        assert [call[3] for call in _created(window, STATIC_CLASS)] == ["Startup"]
        assert [call[3] for call in _created_with_style(window, BUTTON_CLASS, BS_GROUPBOX)] == [""]

    def test_the_tab_control_is_pushed_behind_its_pages(self):
        # A child created later goes to the bottom of the z-order, so without
        # this the tab control sits over every page and they never appear.
        window = _window()
        window._create()

        sent_back = [
            call[1]
            for call in window._user32.named("SetWindowPos")
            if call[2] == HWND_BOTTOM
        ]
        assert sent_back == [window._tab_handle]

    def test_the_three_dialog_buttons_are_created(self):
        window = _window()
        window._create()

        labels = [call[3] for call in _created(window, BUTTON_CLASS)]
        assert {"OK", "Cancel", "Apply"} <= set(labels)

    def test_every_control_is_given_the_dialog_font(self):
        window = _window()
        window._create()

        fonts = [call for call in window._user32.named("SendMessageW") if call[2] == WM_SETFONT]
        assert len(fonts) >= len(_created(window, BUTTON_CLASS))

    def test_an_unavailable_setting_is_shown_but_disabled(self):
        field, _state = _toggle_field(available=False)
        window = _window(_one_tab(field))
        window._create()

        handle = window._controls["taskbar"].label
        assert window._user32.enabled[handle] is False

    def test_two_fields_that_read_the_same_get_command_ids_of_their_own(self):
        # A field is a frozen dataclass, so two of them that were wired the
        # same way compare equal. Routing by position rather than by identity
        # is what keeps the second one from answering to the first one's id.
        switch, _state = _switch()
        first = replace(switch, key="taskbar", label="Same label")
        second = replace(switch, key="codex", label="Same label")
        assert first != second  # only the key tells them apart
        window = _window(_one_tab(first, second))

        window._create()

        assert window._controls["taskbar"].label != window._controls["codex"].label

    def test_common_controls_are_registered_before_the_tab_is_made(self):
        window = _window()
        window._create()

        assert window._comctl32.was_called("InitCommonControlsEx")


class TestWhichPageIsShown:
    """One page at a time; the others are hidden rather than destroyed."""

    def test_only_the_first_page_is_visible_at_first(self):
        window = _window()
        window._create()

        assert window._user32.shown[window._page_handles[0]] == SW_SHOW
        assert window._user32.shown[window._page_handles[1]] == SW_HIDE

    def test_changing_tab_shows_the_new_page_and_hides_the_old(self):
        window = _window()
        window._create()

        _switch_to_tab(window, 1)

        assert window._user32.shown[window._page_handles[1]] == SW_SHOW
        assert window._user32.shown[window._page_handles[0]] == SW_HIDE

    def test_a_notification_from_something_else_is_ignored(self):
        window = _window()
        window._create()
        header = NMHDR()
        header.hwndFrom = 999999
        header.code = TCN_SELCHANGE

        window._window_proc(window._handle, WM_NOTIFY, 0, ctypes.addressof(header))

        assert window._user32.shown[window._page_handles[0]] == SW_SHOW

    def test_a_tab_index_the_dialog_does_not_have_is_ignored(self):
        window = _window()
        window._create()
        shown_before = dict(window._user32.shown)

        _switch_to_tab(window, 9)

        assert window._user32.shown == shown_before


class TestNothingIsWrittenUntilApplied:
    """The whole point of OK / Cancel / Apply, and the break from the old window."""

    def _window_with_switch(self):
        field, state = _toggle_field(on=True)
        window = _window(_one_tab(field))
        window._create()
        return window, state

    def _click_checkbox(self, window, key: str, checked: bool) -> None:
        """Tick or untick a box the way Windows does, then tell the window."""
        handle = window._controls[key].label
        window._user32.checked[handle] = BST_CHECKED if checked else BST_UNCHECKED
        index = [field.key for field in window._model.fields()].index(key)
        _command(window, _FIRST_FIELD_ID + index)

    def test_ticking_a_box_writes_nothing(self):
        window, state = self._window_with_switch()

        self._click_checkbox(window, "taskbar", False)

        assert state["on"] is True

    def test_ok_writes_every_change(self):
        window, state = self._window_with_switch()
        self._click_checkbox(window, "taskbar", False)

        _command(window, IDOK)

        assert state["on"] is False

    def test_ok_closes_the_window(self):
        window, _state = self._window_with_switch()

        _command(window, IDOK)

        assert window._user32.was_called("DestroyWindow")

    def test_cancel_writes_nothing_and_closes(self):
        window, state = self._window_with_switch()
        self._click_checkbox(window, "taskbar", False)

        _command(window, IDCANCEL)

        assert state["on"] is True
        assert window._user32.was_called("DestroyWindow")

    def test_apply_writes_every_change_and_stays_open(self):
        window, state = self._window_with_switch()
        self._click_checkbox(window, "taskbar", False)

        _command(window, _APPLY_ID)

        assert state["on"] is False
        assert not window._user32.was_called("DestroyWindow")

    def test_closing_the_title_bar_discards_the_changes(self):
        window, state = self._window_with_switch()
        self._click_checkbox(window, "taskbar", False)

        window._window_proc(window._handle, WM_CLOSE, 0, 0)

        assert state["on"] is True

    def test_applying_twice_writes_once(self):
        written: list[bool] = []
        field = SettingToggle(
            key="taskbar",
            label="Show usage in the taskbar",
            is_on=lambda: True,
            write=written.append,
        )
        window = _window(_one_tab(field))
        window._create()
        self._click_checkbox(window, "taskbar", False)

        _command(window, _APPLY_ID)
        _command(window, _APPLY_ID)

        assert written == [False]

    def test_a_setting_that_refuses_to_save_is_logged_not_raised(self, caplog):
        def refuse(value):
            raise OSError("the registry is locked")

        field = SettingToggle(
            key="startup", label="Start with Windows", is_on=lambda: False, write=refuse
        )
        window = _window(_one_tab(field))
        window._create()
        self._click_checkbox(window, "startup", True)

        with caplog.at_level(logging.ERROR):
            _command(window, _APPLY_ID)

        assert "startup" in caplog.text


class TestTypingANumber:
    """A number box is read back as text, so every value passes through parsing."""

    def _window_with_number(self, **kwargs):
        field, state = _number_field(**kwargs)
        window = _window(_one_tab(field))
        window._create()
        return window, state

    def _type(self, window, key: str, text: str) -> None:
        """Put text in the box and send the change notification Windows would."""
        window._user32.text[window._controls[key].editor] = text
        index = [field.key for field in window._model.fields()].index(key)
        _command(window, _FIRST_EDITOR_ID + index, EN_CHANGE)

    def test_a_typed_number_is_written_on_ok(self):
        window, state = self._window_with_number(value=60)

        self._type(window, "poll_interval", "120")
        _command(window, IDOK)

        assert state["value"] == 120

    def test_a_number_outside_the_range_is_clamped(self):
        window, state = self._window_with_number(value=60, minimum=10, maximum=600)

        self._type(window, "poll_interval", "9999")
        _command(window, IDOK)

        assert state["value"] == 600

    def test_a_half_typed_box_does_not_change_the_setting(self):
        window, state = self._window_with_number(value=60)

        self._type(window, "poll_interval", "")
        _command(window, IDOK)

        assert state["value"] == 60

    def test_applying_puts_the_stored_value_back_in_the_box(self):
        # A clamped number has to become visible, or the box keeps claiming a
        # value the setting never accepted.
        window, _state = self._window_with_number(value=60, minimum=10, maximum=600)
        self._type(window, "poll_interval", "9999")

        _command(window, _APPLY_ID)

        assert window._user32.text[window._controls["poll_interval"].editor] == "600"

    def test_a_box_that_reports_before_it_exists_is_ignored(self):
        # Windows announces an edit control's first text while
        # CreateWindowExW is still running, before there is a handle for it.
        field, state = _number_field(value=60)
        window = _window(_one_tab(field))
        window._create()
        window._controls.clear()

        _command(window, _FIRST_EDITOR_ID, EN_CHANGE)
        _command(window, IDOK)

        assert state["value"] == 60

    def test_the_window_ignores_the_change_it_causes_itself(self):
        # Putting the value back fires EN_CHANGE again; treating that as a user
        # edit would make the window permanently dirty.
        window, _state = self._window_with_number(value=60)
        self._type(window, "poll_interval", "120")
        _command(window, _APPLY_ID)

        assert window._pending.is_dirty() is False


class TestColours:
    """Three surfaces: the dialog behind the buttons, the page, and a text box."""

    def test_a_control_on_the_page_is_given_the_page_colour(self):
        # A control asks its own parent for its colours, and its parent is the
        # page window rather than the frame.
        window = _window()
        window._create()

        window._window_proc(window._page_handles[0], WM_CTLCOLORSTATIC, 500, 0)

        assert ("SetBkColor", 500, LIGHT_THEME_PAGE_BACKGROUND) in window._gdi32.calls

    def test_a_number_box_is_given_the_text_field_colour(self):
        window = _window()
        window._create()

        window._window_proc(window._page_handles[0], WM_CTLCOLOREDIT, 500, 0)

        assert ("SetBkColor", 500, LIGHT_THEME_FIELD_BACKGROUND) in window._gdi32.calls

    def test_a_page_paints_its_own_background(self):
        window = _window()
        window._create()

        assert window._window_proc(window._page_handles[0], WM_ERASEBKGND, 500, 0) == 1
        assert ("FillRect", 500, ANY_RECT, window._page_brush) in _fills(window)

    def test_a_click_on_a_page_reaches_the_dialog(self):
        # A control reports to its own parent, so without the hand-off nothing
        # on a page would ever be heard.
        field, state = _toggle_field(on=True)
        window = _window(_one_tab(field))
        window._create()
        window._user32.checked[window._controls["taskbar"].label] = BST_UNCHECKED

        window._window_proc(window._page_handles[0], WM_COMMAND, _FIRST_FIELD_ID, 0)
        _command(window, IDOK)

        assert state["on"] is False

    def test_a_dialog_button_is_given_the_dialog_colour(self):
        window = _window()
        window._create()

        window._window_proc(window._handle, WM_CTLCOLORSTATIC, 500, 0)

        assert ("SetBkColor", 500, LIGHT_THEME_BACKGROUND) in window._gdi32.calls

    def test_the_dark_page_colour_is_used_in_dark_mode(self):
        window = _window(light=False)
        window._create()

        window._window_proc(window._page_handles[0], WM_CTLCOLORSTATIC, 500, 0)

        assert ("SetBkColor", 500, DARK_THEME_PAGE_BACKGROUND) in window._gdi32.calls

    def test_dark_mode_asks_for_a_dark_title_bar(self):
        window = _window(light=False)
        window._create()

        assert window._dwmapi.was_called("DwmSetWindowAttribute")

    def test_light_mode_leaves_the_title_bar_alone(self):
        window = _window(light=True)
        window._create()

        assert not window._dwmapi.was_called("DwmSetWindowAttribute")


class TestTheWindowLifecycle:
    """Showing, raising, and closing, unchanged in spirit from the old dialog."""

    def test_showing_creates_the_window_and_pumps_until_it_closes(self):
        window = _window()

        window.show()

        assert window._user32.was_called("CreateWindowExW")
        assert window._user32.was_called("GetMessageW")

    def test_the_handle_is_forgotten_once_the_window_closes(self):
        window = _window()

        window.show()

        assert window._handle is None

    def test_the_font_and_brushes_are_released(self):
        window = _window()

        window.show()

        assert len(window._gdi32.named("DeleteObject")) >= 2

    def test_focus_restores_and_raises_the_window(self):
        window = _window()
        window._create()

        assert window.focus() == 1
        assert window._user32.was_called("SetForegroundWindow")

    def test_focus_on_a_window_that_was_never_created_is_false(self):
        assert _window().focus() is False

    def test_close_posts_a_message_rather_than_destroying_directly(self):
        # Only the thread that made a window may destroy it, and close() is
        # called from the one shutting the application down.
        window = _window()
        window._create()

        window.close()

        assert [call[2] for call in window._user32.named("PostMessageW")] == [WM_CLOSE]

    def test_a_broken_message_pump_ends_the_loop(self, caplog):
        # GetMessage answers -1 on an error. Treating that as "a message
        # arrived" would spin this thread flat out for the life of the app.
        window = _window()
        window._user32.message_result = -1

        with caplog.at_level(logging.WARNING):
            window.show()

        assert "settings window" in caplog.text

    def test_a_link_button_opens_its_target(self):
        opened: list[str] = []
        link = SettingLink(key="logs", label="Open log folder", open=lambda: opened.append("logs"))
        window = _window(_one_tab(link))
        window._create()

        _command(window, _FIRST_FIELD_ID)

        assert opened == ["logs"]

    def test_a_link_that_fails_is_logged_not_raised(self, caplog):
        def refuse():
            raise OSError("no such folder")

        window = _window(_one_tab(SettingLink(key="logs", label="Open log folder", open=refuse)))
        window._create()

        with caplog.at_level(logging.ERROR):
            _command(window, _FIRST_FIELD_ID)

        assert "Open log folder" in caplog.text


class TestTheDarkTabStrip:
    """SysTabControl32 has no dark rendering, so in dark mode it is painted here.

    Left to itself it draws a white strip and a white line round the page, on
    a dialog that is otherwise near-black — which is worse than either theme.
    """

    def _dark_window(self):
        window = _window(light=False)
        window._create()
        return window

    def test_the_tab_control_is_subclassed_in_dark_mode(self):
        window = self._dark_window()

        subclassed = [
            call
            for call in window._user32.named("SetWindowLongPtrW")
            if call[1] == window._tab_handle and call[2] == GWLP_WNDPROC
        ]
        assert len(subclassed) == 1

    def test_the_tab_control_is_left_alone_in_light_mode(self):
        # The native strip is the right answer wherever Windows will draw one.
        window = _window(light=True)
        window._create()

        assert not [
            call
            for call in window._user32.named("SetWindowLongPtrW")
            if call[2] == GWLP_WNDPROC
        ]

    def test_the_strip_is_filled_with_the_dialog_background(self):
        window = self._dark_window()
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        assert _brushes_filled(window)[0] == window._background_brush

    def test_the_current_tab_is_filled_with_the_page_colour(self):
        # The selected tab has to read as part of the page below it.
        window = self._dark_window()
        window._user32.results["SendMessageW"] = lambda handle, message, wparam, lparam: (
            3 if message == TCM_GETITEMCOUNT else 0
        )
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        assert window._page_brush in _brushes_filled(window)

    def test_the_other_tabs_are_filled_with_the_inactive_colour(self):
        window = self._dark_window()
        window._user32.results["SendMessageW"] = lambda handle, message, wparam, lparam: (
            3 if message == TCM_GETITEMCOUNT else 0
        )
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        assert window._tab_inactive_brush in _brushes_filled(window)

    def test_every_tab_is_titled(self):
        window = self._dark_window()
        window._user32.results["SendMessageW"] = lambda handle, message, wparam, lparam: (
            3 if message == TCM_GETITEMCOUNT else 0
        )
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        titled = [call[2] for call in window._user32.named("DrawTextW")]
        assert titled == ["General", "Providers", "Taskbar"]

    def test_the_page_is_outlined(self):
        window = self._dark_window()
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        assert window._border_brush in _brushes_framed(window)

    def test_the_dark_brushes_are_released_with_the_others(self):
        window = _window(light=False)

        window.show()

        # Background, page, field, inactive tab, and border.
        assert len(window._gdi32.named("DeleteObject")) >= 5

    def test_light_mode_makes_no_tab_strip_brushes(self):
        window = _window(light=True)
        window._create()

        assert window._tab_inactive_brush is None
        assert window._border_brush is None

    def test_painting_asks_the_control_where_its_tabs_are(self):
        # The control still lays the strip out; only the pixels are ours.
        window = self._dark_window()
        window._user32.results["SendMessageW"] = lambda handle, message, wparam, lparam: (
            2 if message == TCM_GETITEMCOUNT else 0
        )
        window._user32.calls.clear()

        window._paint_tab_strip(500)

        asked = [
            call[3]
            for call in window._user32.named("SendMessageW")
            if call[2] == TCM_GETITEMRECT
        ]
        # Each tab once to draw it, then the current one again to join it to
        # the page below.
        assert asked == [0, 1, 0]

    def test_the_font_is_put_back_after_painting(self):
        # The device context belongs to Windows; a font left selected in it
        # outlives the paint and leaks into whatever draws next.
        window = self._dark_window()
        window._font = 42
        window._gdi32.results["SelectObject"] = 77  # Whatever was there before.
        window._gdi32.calls.clear()

        window._paint_tab_strip(500)

        selections = [call[2] for call in window._gdi32.named("SelectObject")]
        assert selections == [42, 77]
