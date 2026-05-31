from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel

_DEFAULT_TOML = """\
# ClaudeMonitor config — edit and restart the app

[polling]
# How often to check Anthropic for usage updates, in seconds.
interval_seconds = 30

[thresholds]
# 5h-window % remaining at which the icon turns amber and red.
amber_below = 50
red_below   = 20
"""


class PollingConfig(BaseModel):
    interval_seconds: int = 30


class ThresholdsConfig(BaseModel):
    amber_below: float = 50
    red_below: float = 20


class Config(BaseModel):
    polling: PollingConfig = PollingConfig()
    thresholds: ThresholdsConfig = ThresholdsConfig()


def _config_path() -> Path:
    return Path(os.environ["APPDATA"]) / "claudemonitor" / "config.toml"


def load_config() -> Config:
    path = _config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_TOML, encoding="utf-8")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    polling = PollingConfig(**raw.get("polling", {}))
    thresholds = ThresholdsConfig(**raw.get("thresholds", {}))
    return Config(polling=polling, thresholds=thresholds)
