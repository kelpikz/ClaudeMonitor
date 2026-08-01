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
# ClaudeMonitor config — edit and restart the app

[polling]
# How often to check Anthropic for usage updates, in seconds.
interval_seconds = 60

[thresholds]
# 5h-window % remaining at which the icon turns amber and red.
amber_below = 50
red_below   = 20

[taskbar]
# Show the compact Claude usage summary in the Windows taskbar.
enabled = true
"""


class PollingConfig(BaseModel):
    interval_seconds: int = 60


class ThresholdsConfig(BaseModel):
    amber_below: float = 50
    red_below: float = 20


class TaskbarConfig(BaseModel):
    enabled: bool = True


class Config(BaseModel):
    polling: PollingConfig = PollingConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    taskbar: TaskbarConfig = TaskbarConfig()


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


def save_taskbar_enabled(enabled: bool) -> None:
    """Persist taskbar visibility using a comment-preserving TOML document."""
    path = _config_path()
    if not path.exists():
        _seed_default_config(path)
    document = _editable_document(path)
    taskbar = document.get("taskbar")
    if taskbar is None:
        taskbar = tomlkit.table()
        document["taskbar"] = taskbar
    taskbar["enabled"] = enabled
    _write_atomically(path, tomlkit.dumps(document))
