"""Own every setting the user can toggle from the tray.

Each setting has exactly one source of truth, and it is not this module: the
taskbar label's visibility belongs to the companion showing it, the session
nudge belongs to the nudger running it, and Windows startup belongs to the
registry. Reads go through to that owner rather than caching a copy, so a value
changed elsewhere can never leave the tray showing something stale. Persistence,
where a setting has any, happens on the same write.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from . import autostart, config

log = logging.getLogger(__name__)

# Where a setting is written when it also lives in the config file.
Saver = Callable[[str, str, object], None]


class VisibilityOwner(Protocol):
    """Something owning a live visible flag — the taskbar companion."""

    @property
    def visible(self) -> bool: ...
    def set_visible(self, visible: bool) -> None: ...


class EnabledOwner(Protocol):
    """Something owning a live enabled flag — the session nudger."""

    @property
    def enabled(self) -> bool: ...
    def set_enabled(self, enabled: bool) -> None: ...


class Setting:
    """One boolean the user can toggle from the tray.

    Neither reading nor writing ever raises. Both happen inside pystray's
    message loop, where an escaping exception surfaces only as a stderr
    traceback nobody sees in a windowed build.
    """

    def __init__(
        self,
        name: str,
        *,
        read: Callable[[], bool],
        write: Callable[[bool], None],
    ) -> None:
        self._name = name
        self._read = read
        self._write = write

    @property
    def enabled(self) -> bool:
        """Return the setting's live value, or False when it cannot be read."""
        try:
            return self._read()
        except Exception:
            log.exception("unable to read %s", self._name)
            return False

    def toggle(self) -> bool:
        """Flip the setting and report where it actually landed.

        The value is re-read afterwards rather than assumed, so a write that
        only half applied is reported honestly.
        """
        try:
            self._write(not self.enabled)
        except Exception:
            log.exception("unable to update %s", self._name)
        return self.enabled


def _resolve(override: object | None, default: object) -> object:
    """Pick an injected collaborator, falling back to the production one.

    Resolved when the setting is built rather than bound as a default argument
    at import time, so the seam stays substitutable for tests.
    """
    return default if override is None else override


def taskbar_setting(
    companion: VisibilityOwner,
    save: Saver | None = None,
) -> Setting:
    """Build the taskbar label's visibility setting, owned by the companion."""
    persist: Saver = _resolve(save, config.save_setting)  # type: ignore[assignment]

    def write(enabled: bool) -> None:
        # The live effect lands first: a config file we cannot write must not
        # cost the user a toggle they have already watched take effect.
        companion.set_visible(enabled)
        persist("taskbar", "enabled", enabled)

    return Setting("taskbar visibility", read=lambda: companion.visible, write=write)


def session_refresh_setting(
    nudger: EnabledOwner,
    save: Saver | None = None,
) -> Setting:
    """Build the Claude CLI session nudge setting, owned by the nudger."""
    persist: Saver = _resolve(save, config.save_setting)  # type: ignore[assignment]

    def write(enabled: bool) -> None:
        nudger.set_enabled(enabled)
        persist("session_refresh", "enabled", enabled)

    return Setting("session refresh", read=lambda: nudger.enabled, write=write)


def startup_setting(
    is_enabled: Callable[[], bool] | None = None,
    set_enabled: Callable[[bool], None] | None = None,
) -> Setting:
    """Build the Windows startup registration setting, owned by the registry.

    There is nothing to persist separately here — the registry is both the live
    value and the stored one.
    """
    return Setting(
        "Windows startup registration",
        read=_resolve(is_enabled, autostart.is_enabled),  # type: ignore[arg-type]
        write=_resolve(set_enabled, autostart.set_enabled),  # type: ignore[arg-type]
    )
