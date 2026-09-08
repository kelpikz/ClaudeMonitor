from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError
import tomlkit

log = logging.getLogger(__name__)

_DEFAULT_TOML = """\
# ClaudeMonitor config. Everything here is also in the settings window
# (tray icon -> Settings...), which writes this file and takes effect at once.
# Edited by hand, these values are read at the next launch.

[polling]
# How often to check Anthropic for usage updates, in seconds.
interval_seconds = 60

[thresholds]
# 5h-window % remaining at which the icon turns amber and red.
amber_below = 50
red_below   = 20

[taskbar]
# Show the compact usage summary in the Windows taskbar.
enabled = true

[codex]
# Track OpenAI Codex usage alongside Claude: a second tray icon, and a
# second reading in the taskbar label. Reads ~/.codex/auth.json, which the
# Codex CLI writes when you log in.
enabled = true

[session_refresh]
# When the token has expired or the 5h window is completely untouched, run
# `claude -p --model haiku "hi"` so Claude Code renews the token and opens the
# session. Costs a negligible amount of usage; disable to never spend any.
enabled = true
# Shortest gap between two such calls, in seconds.
cooldown_seconds = 900
"""


class PollingConfig(BaseModel):
    interval_seconds: int = 60


class ThresholdsConfig(BaseModel):
    amber_below: float = 50
    red_below: float = 20


class TaskbarConfig(BaseModel):
    enabled: bool = True


class CodexConfig(BaseModel):
    enabled: bool = True


class SessionRefreshConfig(BaseModel):
    enabled: bool = True
    cooldown_seconds: float = 900


class Config(BaseModel):
    polling: PollingConfig = PollingConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    taskbar: TaskbarConfig = TaskbarConfig()
    codex: CodexConfig = CodexConfig()
    session_refresh: SessionRefreshConfig = SessionRefreshConfig()


_Section = TypeVar("_Section", bound=BaseModel)


def _config_path() -> Path:
    return Path(os.environ["APPDATA"]) / "claudemonitor" / "config.toml"


def _write_atomically(path: Path, text: str) -> None:
    """Replace a file's contents in one step so a crash cannot truncate it."""
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_text(text, encoding="utf-8")
    try:
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def _seed_default_config(path: Path) -> None:
    """Create the commented starter config the user is expected to edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(path, _DEFAULT_TOML)


def _read_toml(path: Path) -> dict:
    """Parse the config file, treating an unreadable one as 'no settings'."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        log.warning("unreadable config at %s (%s) — using defaults", path, exc)
        return {}


def _section(model: Type[_Section], raw: dict, name: str) -> _Section:
    """Build one config section, falling back to its defaults if it is invalid."""
    try:
        return model(**raw.get(name, {}))
    except (ValidationError, TypeError) as exc:
        log.warning("invalid [%s] config section (%s) — using defaults", name, exc)
        return model()


def load_config() -> Config:
    """Read the user's config, seeding a default file the first time it runs."""
    path = _config_path()
    if not path.exists():
        _seed_default_config(path)
    raw = _read_toml(path)
    return Config(
        polling=_section(PollingConfig, raw, "polling"),
        thresholds=_section(ThresholdsConfig, raw, "thresholds"),
        taskbar=_section(TaskbarConfig, raw, "taskbar"),
        codex=_section(CodexConfig, raw, "codex"),
        session_refresh=_section(SessionRefreshConfig, raw, "session_refresh"),
    )


def _editable_document(path: Path) -> tomlkit.TOMLDocument:
    """Load the config for editing, starting over if it cannot be parsed.

    ``load_config`` already falls back to defaults on a malformed file, so the
    writer has to agree: otherwise the app runs happily on defaults while every
    settings change is silently discarded.
    """
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("config at %s is unparseable (%s) — rewriting defaults", path, exc)
        return tomlkit.parse(_DEFAULT_TOML)


def _save_setting(section_name: str, key: str, value: object) -> None:
    """Write one setting back, preserving the comments around every other one."""
    path = _config_path()
    if not path.exists():
        _seed_default_config(path)
    document = _editable_document(path)
    section = document.get(section_name)
    if section is None:
        section = tomlkit.table()
        document[section_name] = section
    section[key] = value
    _write_atomically(path, tomlkit.dumps(document))


def save_taskbar_enabled(enabled: bool) -> None:
    """Persist whether the taskbar usage label is shown."""
    _save_setting("taskbar", "enabled", enabled)


def save_session_refresh_enabled(enabled: bool) -> None:
    """Persist whether an idle session may be woken with a Claude CLI prompt."""
    _save_setting("session_refresh", "enabled", enabled)


def save_codex_enabled(enabled: bool) -> None:
    """Persist whether Codex usage is tracked alongside Claude."""
    _save_setting("codex", "enabled", enabled)


def save_poll_interval_seconds(seconds: int) -> None:
    """Persist how often usage is fetched."""
    _save_setting("polling", "interval_seconds", seconds)


def save_amber_threshold(percent: float) -> None:
    """Persist the remaining percentage below which the icon turns amber."""
    _save_setting("thresholds", "amber_below", percent)


def save_red_threshold(percent: float) -> None:
    """Persist the remaining percentage below which the icon turns red."""
    _save_setting("thresholds", "red_below", percent)


def save_session_refresh_cooldown(seconds: float) -> None:
    """Persist the shortest gap allowed between two CLI session nudges."""
    _save_setting("session_refresh", "cooldown_seconds", seconds)
