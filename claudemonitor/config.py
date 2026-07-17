from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel

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


def _config_path() -> Path:
    return Path(os.environ["APPDATA"]) / "claudemonitor" / "config.toml"


def load_config() -> Config:
    path = _config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_TOML, encoding="utf-8")
        toml_text = _DEFAULT_TOML
    else:
        toml_text = path.read_text(encoding="utf-8")
    raw = tomllib.loads(toml_text)
    polling = PollingConfig(**raw.get("polling", {}))
    thresholds = ThresholdsConfig(**raw.get("thresholds", {}))
    taskbar = TaskbarConfig(**raw.get("taskbar", {}))
    return Config(polling=polling, thresholds=thresholds, taskbar=taskbar)


def save_taskbar_enabled(enabled: bool) -> None:
    """Persist taskbar visibility while preserving the user's other TOML settings."""
    path = _config_path()
    if not path.exists():
        load_config()
    text = path.read_text(encoding="utf-8")
    value = "true" if enabled else "false"
    section = re.search(r"(?ms)^\[taskbar\]\s*$.*?(?=^\[|\Z)", text)
    if section is None:
        text = text.rstrip() + f"\n\n[taskbar]\nenabled = {value}\n"
    else:
        block = section.group(0)
        if re.search(r"(?m)^\s*enabled\s*=", block):
            updated = re.sub(
                r"(?m)^(\s*enabled\s*=\s*)(?:true|false)(\s*(?:#.*)?)$",
                rf"\g<1>{value}\g<2>",
                block,
            )
        else:
            updated = block.rstrip() + f"\nenabled = {value}\n"
        text = text[: section.start()] + updated + text[section.end() :]
    path.write_text(text, encoding="utf-8")
