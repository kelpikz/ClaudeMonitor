"""Manage ClaudeMonitor's per-user Windows startup registration."""

from __future__ import annotations

import subprocess
import sys
import winreg
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "ClaudeMonitor"


def startup_command(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
    launcher: Path | None = None,
) -> str:
    """Build the command Windows should run after the current user signs in."""
    executable = executable or Path(sys.executable)
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        return subprocess.list2cmdline([str(executable)])

    launcher = launcher or Path(__file__).resolve().parents[1] / "run.py"
    pythonw = executable.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else executable
    return subprocess.list2cmdline([str(interpreter), str(launcher)])


def _registered_command() -> str | None:
    """Read the current user's startup command, or return None when absent."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_READ,
        )
    except FileNotFoundError:
        return None
    try:
        try:
            value, _value_type = winreg.QueryValueEx(key, _VALUE_NAME)
        except FileNotFoundError:
            return None
        return str(value)
    finally:
        winreg.CloseKey(key)


def is_enabled() -> bool:
    """Return whether Windows is registered to launch this app command."""
    return _registered_command() == startup_command()


def set_enabled(enabled: bool) -> None:
    """Add or remove ClaudeMonitor from the current user's startup programs."""
    if enabled:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.SetValueEx(
                key,
                _VALUE_NAME,
                0,
                winreg.REG_SZ,
                startup_command(),
            )
        finally:
            winreg.CloseKey(key)
        return

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
    except FileNotFoundError:
        return
    try:
        try:
            winreg.DeleteValue(key, _VALUE_NAME)
        except FileNotFoundError:
            pass
    finally:
        winreg.CloseKey(key)


def repair_if_enabled() -> bool:
    """Update a stale registered path without enabling an opted-out install."""
    registered = _registered_command()
    if registered is None:
        return False
    expected = startup_command()
    if registered != expected:
        set_enabled(True)
    return True
