from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray

from . import fetcher, processor, tray
from .config import load_config

_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance(name: str = "ClaudeMonitor.SingleInstance") -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, name)
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "claudemonitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)],
    )


def main() -> None:
    log_dir = Path(os.environ["APPDATA"]) / "claudemonitor"
    _setup_logging(log_dir)

    if not _acquire_single_instance():
        sys.exit(0)

    log = logging.getLogger(__name__)
    log.info("ClaudeMonitor starting")

    cfg = load_config()
    manual_refresh = threading.Event()
    tray.init(manual_refresh, log_dir)

    def setup(icon: pystray.Icon) -> None:
        icon.visible = True
        # Remember the most recent successful fetch so a later rate-limit (429)
        # can keep showing real usage instead of a grey "offline" icon.
        last_good: fetcher.AnthropicUsageData | None = None
        while True:
            try:
                data = fetcher.fetch()
                if data.fetch_error is None and data.five_hour is not None:
                    last_good = data
                state = processor.process(
                    data, now=datetime.now(timezone.utc), config=cfg, last_good=last_good
                )
            except Exception:
                log.exception("unhandled error in poll loop")
                state = processor.internal_error_state(now=datetime.now(timezone.utc))
            tray.apply(icon, state)
            manual_refresh.clear()
            manual_refresh.wait(timeout=cfg.polling.interval_seconds)

    icon = pystray.Icon(
        "ClaudeMonitor",
        icon=tray.loading_icon(),
        title="Claude Monitor — loading…",
        menu=pystray.Menu(),
    )
    icon.run(setup=setup)


def poll() -> None:
    import json
    data = fetcher.fetch()
    print(json.dumps(data.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
