"""What the settings window contains, and the rule that there is one of it.

The tray menu used to carry every app-wide switch. Each new provider added
another one, until the menu was longer than the thing it configured. This
module says which tabs exist, which groups sit on each tab, and what every
field reads and writes; the Windows drawing is in ``win32_settings_window``,
behind the small ``SettingsView`` protocol below, so the content can be tested
without creating a window.

Nothing here writes as it is clicked. The window edits a ``PendingSettings``
buffer and the buffer reaches the real settings only on OK or Apply, which is
what makes Cancel a word this dialog can honour.
"""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Protocol, Union

from .models import Provider

log = logging.getLogger(__name__)

WINDOW_TITLE = "Claude Monitor settings"


def _open_in_browser(url: str) -> None:
    """Open one page in whatever the user's default browser is."""
    webbrowser.open(url)


def _open_in_explorer(path: str) -> None:
    """Open one folder in Explorer."""
    os.startfile(path)


def _always_available() -> bool:
    """Most settings can always be changed; a few have to answer for themselves."""
    return True


# --------------------------------------------------------------------- a field
# One field is both the wiring and what is drawn around it. The caller builds
# it with the wiring alone — a way to read the setting and a way to write it —
# and ``build_settings`` names it, because every user-facing string about
# settings belongs in this file rather than in whoever owns the setting.
#
# The window never toggles anything, because a buffered edit has to be able to
# say "set this to False" twice without the second click undoing the first.


@dataclass(frozen=True)
class SettingToggle:
    """One boolean setting: a labelled checkbox over a value it reads and writes.

    ``available`` exists for the taskbar label, which can fail to appear at
    all: a checkbox that claims otherwise is a lie the user cannot act on.
    """

    is_on: Callable[[], bool]
    write: Callable[[bool], None]
    available: Callable[[], bool] = _always_available
    key: str = ""
    label: str = ""


@dataclass(frozen=True)
class SettingNumber:
    """One numeric setting: a labelled box, its range, and the unit after it."""

    value: Callable[[], int]
    write: Callable[[int], None]
    minimum: int
    maximum: int
    key: str = ""
    label: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class ProviderFields:
    """One provider's own box on the Providers tab.

    ``tracking`` is the switch that turns the provider off, and is ``None``
    for a provider the application always shows. The box is built from the
    ``Provider`` itself, so a third provider adds no code to this module.
    """

    provider: Provider
    tracking: SettingToggle | None = None


@dataclass(frozen=True)
class SettingLink:
    """A labelled button that opens something outside the application."""

    key: str
    label: str
    open: Callable[[], None]


SettingField = Union[SettingToggle, SettingNumber, SettingLink]
EditableField = Union[SettingToggle, SettingNumber]


@dataclass(frozen=True)
class SettingsGroup:
    """One captioned box of related fields, as the window draws it."""

    title: str
    fields: list[SettingField] = field(default_factory=list)


@dataclass(frozen=True)
class SettingsTab:
    """One page of the tabbed dialog."""

    title: str
    groups: list[SettingsGroup] = field(default_factory=list)


@dataclass(frozen=True)
class SettingsModel:
    """Everything the settings window shows, in the order it shows it."""

    tabs: list[SettingsTab] = field(default_factory=list)

    def fields(self) -> list[SettingField]:
        """Every field on every tab, flattened into the order they are drawn.

        The window needs one control id per field and does not care which box
        a field sits in, so it walks this rather than the tree.
        """
        return [
            item for tab in self.tabs for group in tab.groups for item in group.fields
        ]

    def editable(self) -> list[EditableField]:
        """Only the fields that hold a value, which is what a pending edit means."""
        return [
            item
            for item in self.fields()
            if isinstance(item, (SettingToggle, SettingNumber))
        ]

    def links(self) -> list[SettingLink]:
        """Only the buttons that open something, in drawing order."""
        return [item for item in self.fields() if isinstance(item, SettingLink)]


def parse_number(field: SettingNumber, text: str) -> int | None:
    """Read what the user typed, held to the range the setting accepts.

    An empty or unparseable box means the user is part-way through typing, so
    there is no new value yet and the last usable one stands.
    """
    try:
        typed = int(text.strip())
    except ValueError:
        return None
    return max(field.minimum, min(field.maximum, typed))


def build_settings(
    *,
    taskbar: SettingToggle,
    providers: list[ProviderFields],
    session_refresh: SettingToggle,
    startup: SettingToggle,
    poll_interval: SettingNumber,
    amber_threshold: SettingNumber,
    red_threshold: SettingNumber,
    refresh_cooldown: SettingNumber,
    log_dir: Path,
    open_url: Callable[[str], None] | None = None,
    open_folder: Callable[[str], None] | None = None,
) -> SettingsModel:
    """Describe the settings window: three tabs of grouped fields.

    The wording is here rather than in the caller so every user-facing string
    about settings stays in one file, as the tray's own labels do.
    """
    # Resolved here rather than as default arguments, which would bind the
    # originals at import and leave nothing for a test to replace.
    open_page: Callable[[str], None] = open_url or _open_in_browser
    open_directory: Callable[[str], None] = open_folder or _open_in_explorer
    return SettingsModel(
        tabs=[
            SettingsTab(
                title="General",
                groups=[
                    SettingsGroup(
                        title="Startup",
                        fields=[
                            _toggle("startup", "Start with Windows", startup),
                        ],
                    ),
                    SettingsGroup(
                        title="Polling",
                        fields=[
                            _number(
                                "poll_interval",
                                "Check usage every",
                                "seconds",
                                poll_interval,
                            ),
                        ],
                    ),
                    SettingsGroup(
                        title="Icon colour",
                        fields=[
                            _number(
                                "amber_threshold",
                                "Turn amber below",
                                "%",
                                amber_threshold,
                            ),
                            _number(
                                "red_threshold", "Turn red below", "%", red_threshold
                            ),
                        ],
                    ),
                    SettingsGroup(
                        title="Logs",
                        fields=[
                            SettingLink(
                                key="log_folder",
                                label="Open log folder",
                                open=lambda: open_directory(str(log_dir)),
                            ),
                        ],
                    ),
                ],
            ),
            SettingsTab(
                title="Providers",
                groups=[
                    SettingsGroup(
                        title="Sessions",
                        fields=[
                            _toggle(
                                "session_refresh",
                                "Auto-refresh idle sessions",
                                session_refresh,
                            ),
                            _number(
                                "refresh_cooldown",
                                "Wait between refreshes",
                                "seconds",
                                refresh_cooldown,
                            ),
                        ],
                    ),
                    *[_provider_group(entry, open_page) for entry in providers],
                ],
            ),
            SettingsTab(
                title="Taskbar",
                groups=[
                    SettingsGroup(
                        title="Taskbar label",
                        fields=[
                            _toggle("taskbar", "Show usage in the taskbar", taskbar),
                        ],
                    ),
                ],
            ),
        ],
    )


def _provider_group(
    entry: ProviderFields,
    open_page: Callable[[str], None],
) -> SettingsGroup:
    """Build one provider's box: its tracking switch, if it has one, and its link.

    The keys are derived from the provider's own key, so two providers can
    never be given the same one and a third needs no new name here.
    """
    provider = entry.provider
    fields: list[SettingField] = []
    if entry.tracking is not None:
        fields.append(
            _toggle(
                f"{provider.key}_tracking",
                f"Track {provider.label} usage",
                entry.tracking,
            )
        )
    fields.append(
        SettingLink(
            key=f"{provider.key}_usage",
            label=f"{provider.label} usage online",
            # Bound now rather than read from the loop variable when clicked.
            open=lambda url=provider.usage_url: open_page(url),
        )
    )
    return SettingsGroup(title=provider.label, fields=fields)


def _toggle(key: str, label: str, toggle: SettingToggle) -> SettingToggle:
    """Give one switch the key and label the window draws it under."""
    return replace(toggle, key=key, label=label)


def _number(key: str, label: str, suffix: str, number: SettingNumber) -> SettingNumber:
    """Give one numeric setting the key, label, and unit the window draws it under."""
    return replace(number, key=key, label=label, suffix=suffix)


class PendingSettings:
    """The values the user has changed but not yet asked to keep.

    Reading falls through to the real setting until something is edited, so a
    freshly opened window shows the truth and a cancelled one leaves no trace.
    """

    def __init__(self, model: SettingsModel) -> None:
        self._fields = {field.key: field for field in model.editable()}
        self._edited: dict[str, bool | int] = {}

    def value_of(self, key: str) -> bool | int | None:
        """Return the pending value if there is one, otherwise the stored one."""
        if key in self._edited:
            return self._edited[key]
        field = self._fields.get(key)
        if field is None:
            return None
        return self._stored(field)

    def edit(self, key: str, value: bool | int) -> None:
        """Record a change, forgetting it again if it matches what is stored.

        Clicking a checkbox twice leaves the setting where it started, and an
        edit that changes nothing must not make the window dirty or cause a
        pointless write on Apply.
        """
        field = self._fields.get(key)
        if field is None:
            return
        if value == self._stored(field):
            self._edited.pop(key, None)
            return
        self._edited[key] = value

    def is_dirty(self) -> bool:
        """Report whether anything would be written by an Apply."""
        return bool(self._edited)

    def discard(self) -> None:
        """Throw every edit away, as Cancel does."""
        self._edited.clear()

    def apply(self) -> None:
        """Write every changed field, then forget the edits.

        One setting that refuses to save must not stop the rest: the write is
        attempted per field and a failure is logged and stepped over. Every
        edit is cleared either way, because the window re-reads the real values
        afterwards and a retained edit would keep claiming a change that the
        setting has already rejected once.
        """
        for key, value in self._edited.items():
            field = self._fields.get(key)
            if field is None:
                continue
            try:
                field.write(value)
            except Exception:
                log.exception("unable to save the %r setting", key)
        self._edited.clear()

    def _stored(self, field: EditableField) -> bool | int:
        """Read one real setting, treating an unreadable one as off or zero."""
        try:
            if isinstance(field, SettingToggle):
                return bool(field.is_on())
            return int(field.value())
        except Exception:
            log.exception("unable to read the %r setting", field.key)
            return False if isinstance(field, SettingToggle) else field.minimum


class SettingsView(Protocol):
    """The window itself, as the controller needs to treat it."""

    def show(self) -> None:
        """Create the window and return only once the user has closed it."""

    def focus(self) -> bool:
        """Bring an open window forward; False if Windows would not."""

    def close(self) -> None:
        """Close the window from another thread, at shutdown."""


def _start_daemon_thread(work: Callable[[], None]) -> None:
    """Run the window on a thread of its own so the tray menu stays responsive."""
    threading.Thread(target=work, name="ClaudeMonitorSettings", daemon=True).start()


class SettingsWindowController:
    """Keep at most one settings window, opened away from the tray's message loop.

    Windows requires a window to be created and pumped on one thread, and
    pystray owns the thread the menu click arrives on — running the window
    there would freeze the menu until the window closed.
    """

    def __init__(
        self,
        *,
        build_window: Callable[[], SettingsView],
        start_background: Callable[[Callable[[], None]], None] = _start_daemon_thread,
    ) -> None:
        self._build_window = build_window
        self._start_background = start_background
        self._lock = threading.Lock()
        self._window: SettingsView | None = None

    def open(self) -> None:
        """Show the settings window, or raise the one already on screen.

        This is called from the tray's own message loop, so nothing here may
        raise: an escaping error would be an invisible stderr traceback in a
        windowed build, and the menu entry would appear to do nothing anyway.
        """
        with self._lock:
            if self._window is not None:
                # A window that exists is never replaced, even when Windows
                # refuses to raise it: it is still on screen, and a second one
                # would be two dialogs disagreeing about the same settings.
                self._raise_existing(self._window)
                return
            try:
                window = self._build_window()
            except Exception:
                log.exception("settings window could not be built")
                return
            self._window = window
        self._start_background(lambda: self._show(window))

    def _raise_existing(self, window: SettingsView) -> None:
        """Bring the open window forward, reporting a refusal rather than acting.

        Windows declines SetForegroundWindow while another application holds
        the foreground lock, and flashes the taskbar button instead. It is
        still the right window; there is nothing further to do about it.
        """
        try:
            if not window.focus():
                log.warning("Windows would not raise the settings window")
        except Exception:
            log.exception("settings window could not be raised")

    def _show(self, window: SettingsView) -> None:
        """Run one window to its close, always letting the next one be opened.

        This is the whole body of a background thread, so an escaping error
        would be invisible in a windowed build — and would also leave the
        controller believing a window it can never focus is still up.
        """
        try:
            window.show()
        except Exception:
            log.exception("settings window could not be shown")
        finally:
            with self._lock:
                if self._window is window:
                    self._window = None

    def close(self) -> None:
        """Close an open settings window, at application shutdown."""
        with self._lock:
            window = self._window
        if window is None:
            return
        try:
            window.close()
        except Exception:
            log.exception("settings window could not be closed")
