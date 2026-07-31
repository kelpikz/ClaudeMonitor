from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel
import tomlkit

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
    raw = tomlkit.parse(toml_text)
    polling = PollingConfig(**raw.get("polling", {}))
    thresholds = ThresholdsConfig(**raw.get("thresholds", {}))
    taskbar = TaskbarConfig(**raw.get("taskbar", {}))
    return Config(polling=polling, thresholds=thresholds, taskbar=taskbar)


def save_taskbar_enabled(enabled: bool) -> None:
    """Persist taskbar visibility using a comment-preserving TOML document."""
    path = _config_path()
    if not path.exists():
        load_config()
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    taskbar = document.get("taskbar")
    if taskbar is None:
        taskbar = tomlkit.table()
        document["taskbar"] = taskbar
    taskbar["enabled"] = enabled
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
