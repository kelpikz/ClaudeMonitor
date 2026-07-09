from __future__ import annotations

from claudemonitor.config import PollingConfig


def test_default_polling_interval_is_one_minute():
    assert PollingConfig().interval_seconds == 60
