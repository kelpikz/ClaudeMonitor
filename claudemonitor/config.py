from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
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

[claude]
# Track Claude usage. Reads ~/.claude/.credentials.json, which Claude Code
# writes when you log in.
enabled = true
# When the token has expired, run the Claude CLI once so it renews it.
renew_token = true
# When the 5h window is not in use, run the Claude CLI once to start it,
# so the tray can count down a real reset time.
wake_session = true
# Shortest gap between two such runs, in seconds.
cooldown_seconds = 900
# The model and reasoning effort for that run. An empty model is the CLI's
# default. An empty effort asks for low.
model = "haiku"
effort = ""

[codex]
# Track OpenAI Codex usage: a second line in the tray tooltip, and a second
# row in the taskbar label. One tray icon serves both.
# Reads ~/.codex/auth.json, which the Codex CLI writes when you log in.
# Off until you turn it on: without Codex, the tray icon would stay grey.
enabled = false
# The same refresh settings as [claude], for the Codex CLI.
renew_token = true
wake_session = true
cooldown_seconds = 900
# Empty uses the model from your own Codex config. The effort replaces the
# one in that config, because a one-word prompt needs little reasoning. An
# empty effort asks for low.
model = ""
effort = "low"
"""


class PollingConfig(BaseModel):
    interval_seconds: int = 60


class ThresholdsConfig(BaseModel):
    amber_below: float = 50
    red_below: float = 20


class TaskbarConfig(BaseModel):
    enabled: bool = True


class ProviderConfig(BaseModel):
    """One provider's section: whether it is tracked, and how its CLI is nudged."""

    enabled: bool = True
    renew_token: bool = True
    wake_session: bool = True
    cooldown_seconds: float = 900
    model: str = ""
    effort: str = ""


class ClaudeConfig(ProviderConfig):
    model: str = "haiku"


class CodexConfig(ProviderConfig):
    # Off by default, so a Claude-only user who updates keeps a green icon
    # rather than a grey "not logged in" row for a CLI they never installed.
    enabled: bool = False
    effort: str = "low"


class Config(BaseModel):
    polling: PollingConfig = PollingConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()
    taskbar: TaskbarConfig = TaskbarConfig()
    claude: ClaudeConfig = ClaudeConfig()
    codex: CodexConfig = CodexConfig()


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


def _legacy_refresh_values(raw: dict) -> dict:
    """Read the old shared [session_refresh] section as provider defaults.

    Before each provider had its own section, one switch and one cooldown
    served both. Only values of the right type are taken, so a broken old
    value cannot cost a provider the rest of its own section.
    """
    legacy = raw.get("session_refresh")
    if not isinstance(legacy, dict):
        return {}
    values: dict = {}
    if isinstance(legacy.get("enabled"), bool):
        values["renew_token"] = legacy["enabled"]
        values["wake_session"] = legacy["enabled"]
    cooldown = legacy.get("cooldown_seconds")
    if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool):
        values["cooldown_seconds"] = cooldown
    return values


def _provider_section(model: Type[_Section], raw: dict, name: str) -> _Section:
    """Build one provider's section on top of what the old shared one said."""
    own = raw.get(name, {})
    merged = {**_legacy_refresh_values(raw), **(own if isinstance(own, dict) else {})}
    return _section(model, {name: merged}, name)


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
        claude=_provider_section(ClaudeConfig, raw, "claude"),
        codex=_provider_section(CodexConfig, raw, "codex"),
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


@dataclass(frozen=True)
class ConfigSetting:
    """One value in config.toml, and the same value in a loaded ``Config``.

    The TOML section names and the ``Config`` attribute names are deliberately
    the same word, so naming the pair once is enough to read it, to change it
    in the running application, and to write it back. Spelling it twice — a
    ``save_*`` wrapper here and an attribute string in the caller — is what
    made a setting something four layers had to agree about.

    ``save`` logs rather than raises. Every caller runs on a UI thread and has
    already changed the running application by the time it writes, so a file
    that cannot be written must not undo that or take the thread down with it.
    """

    section: str
    key: str

    def read(self, config: Config) -> object:
        """Return what a loaded config currently holds for this setting."""
        return getattr(getattr(config, self.section), self.key)

    def write(self, config: Config, value: object) -> None:
        """Change a loaded config, so the running application sees it at once."""
        setattr(getattr(config, self.section), self.key, value)

    def save(self, value: object) -> None:
        """Write this setting to the file, leaving every other one untouched."""
        try:
            _save_setting(self.section, self.key, value)
        except Exception:
            log.exception("unable to persist [%s] %s", self.section, self.key)


@dataclass(frozen=True)
class ProviderSettings:
    """Every writable setting in one provider's section.

    The section is named by the provider's key, so these are made from that
    key rather than spelled once per provider.
    """

    tracking: ConfigSetting
    renew_token: ConfigSetting
    wake_session: ConfigSetting
    cooldown: ConfigSetting
    model: ConfigSetting
    effort: ConfigSetting

    def every(self) -> tuple[ConfigSetting, ...]:
        """Return each setting, for the checks that it really exists."""
        return (
            self.tracking,
            self.renew_token,
            self.wake_session,
            self.cooldown,
            self.model,
            self.effort,
        )


def provider_settings(section: str) -> ProviderSettings:
    """Name the settings of the provider whose section is ``section``."""
    return ProviderSettings(
        tracking=ConfigSetting(section, "enabled"),
        renew_token=ConfigSetting(section, "renew_token"),
        wake_session=ConfigSetting(section, "wake_session"),
        cooldown=ConfigSetting(section, "cooldown_seconds"),
        model=ConfigSetting(section, "model"),
        effort=ConfigSetting(section, "effort"),
    )


POLL_INTERVAL = ConfigSetting("polling", "interval_seconds")
AMBER_THRESHOLD = ConfigSetting("thresholds", "amber_below")
RED_THRESHOLD = ConfigSetting("thresholds", "red_below")
TASKBAR_ENABLED = ConfigSetting("taskbar", "enabled")
CLAUDE_SETTINGS = provider_settings("claude")
CODEX_SETTINGS = provider_settings("codex")

# Every setting the application can write, for the tests that check each one
# still names a section and a key the model has.
EVERY_SETTING = (
    POLL_INTERVAL,
    AMBER_THRESHOLD,
    RED_THRESHOLD,
    TASKBAR_ENABLED,
    *CLAUDE_SETTINGS.every(),
    *CODEX_SETTINGS.every(),
)
