from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from claudemonitor import main
from claudemonitor.config import Config, SessionRefreshConfig
from claudemonitor.models import AnthropicUsageData, DisplayState

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


class _FakeEvent:
    def __init__(self, results: list[bool]):
        self._results = iter(results)
        self.timeouts: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return next(self._results)


class _FakeIcon:
    """Records whether a console shutdown tells pystray to stop."""

    def __init__(self):
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeCompanion:
    def __init__(self, visible: bool = True):
        self.updates: list[tuple[str, str]] = []
        self.visible = visible
        self.healthy = True

    def update(self, text: str, tooltip: str) -> None:
        self.updates.append((text, tooltip))

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class _RecordingPresenter:
    """Absorb the tray half of a display update so only the wiring is asserted."""

    def __init__(self):
        self.applied: list[tuple[object, DisplayState]] = []
        self.notified: list[tuple[str, str]] = []

    def apply(self, icon, state) -> None:
        self.applied.append((icon, state))

    def notify(self, icon, title, message) -> None:
        self.notified.append((title, message))


class _MenuIcon:
    def __init__(self):
        self.menu_updates = 0

    def update_menu(self) -> None:
        self.menu_updates += 1


def test_startup_repair_runs_during_initialization():
    calls: list[None] = []

    main._repair_startup_registration(lambda: calls.append(None) or True)

    assert calls == [None]


def test_startup_repair_survives_a_registry_failure(caplog):
    def unavailable() -> bool:
        raise OSError("registry unavailable")

    with caplog.at_level(logging.ERROR):
        main._repair_startup_registration(unavailable)

    assert "Windows startup registration" in caplog.text


def test_apply_display_updates_tray_and_taskbar():
    presenter = _RecordingPresenter()
    icon = _FakeIcon()
    companion = _FakeCompanion()
    state = DisplayState(
        icon_color="green",
        tooltip="usage",
        menu_status_label="updated",
        taskbar_text="80% (3h 0m)",
    )

    main._apply_display(presenter, icon, state, companion)

    assert presenter.applied == [(icon, state)]
    assert companion.updates == [("80% (3h 0m)", "usage")]


class TestTrayPresenterWiring:
    """The tray menu must reach the settings that own each live value."""

    def _presenter(self, companion, nudger):
        return main.create_tray_presenter(
            manual_refresh=threading.Event(),
            shutdown_requested=threading.Event(),
            log_dir=Path("."),
            companion=companion,
            session_nudger=nudger,
        )

    def test_the_taskbar_menu_entry_hides_the_real_companion(self, monkeypatch):
        saved: list[tuple] = []
        monkeypatch.setattr(
            main.settings.config, "save_setting", lambda *args: saved.append(args)
        )
        companion = _FakeCompanion(visible=True)
        nudger = main.create_session_nudger(Config(), threading.Event())

        self._presenter(companion, nudger)._on_toggle_taskbar(_MenuIcon(), None)

        assert companion.visible is False
        assert saved == [("taskbar", "enabled", False)]

    def test_the_session_refresh_entry_flips_the_real_nudger(self, monkeypatch):
        monkeypatch.setattr(main.settings.config, "save_setting", lambda *args: None)
        nudger = main.create_session_nudger(Config(), threading.Event())

        self._presenter(_FakeCompanion(), nudger)._on_toggle_session_refresh(
            _MenuIcon(), None
        )

        assert nudger.enabled is False


class TestSessionNudgerWiring:
    """A completed CLI refresh must wake the poll loop so the tray shows it at once."""

    def _nudger(self, **session_refresh):
        """Build the production nudger with a synchronous, always-succeeding CLI."""
        cfg = Config(session_refresh=SessionRefreshConfig(**session_refresh))
        manual_refresh = threading.Event()
        nudger = main.create_session_nudger(
            cfg,
            manual_refresh,
            invoke=lambda: True,
            start_background=lambda work: work(),
        )
        return nudger, manual_refresh

    def test_a_successful_refresh_sets_the_manual_refresh_event(self):
        nudger, manual_refresh = self._nudger()

        data = AnthropicUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True
        assert manual_refresh.is_set()

    def test_disabling_the_config_section_disables_the_nudger(self):
        nudger, manual_refresh = self._nudger(enabled=False)

        data = AnthropicUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is False
        assert not manual_refresh.is_set()

    def test_the_configured_cooldown_gates_the_second_attempt(self):
        nudger, _manual_refresh = self._nudger(cooldown_seconds=10_000)

        data = AnthropicUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True
        assert nudger.maybe_nudge(data) is False


def test_wait_refreshes_the_display_each_second_until_next_poll():
    event = _FakeEvent([False, False])
    clock = iter([0.0, 0.0, 1.0, 2.0])
    refreshes: list[None] = []

    refreshed_manually = main._wait_with_display_refresh(
        event,
        interval_seconds=2,
        refresh_display=lambda: refreshes.append(None),
        clock=lambda: next(clock),
    )

    assert refreshed_manually is False
    assert event.timeouts == [1.0, 1.0]
    assert len(refreshes) == 2


def test_wait_stops_immediately_for_manual_refresh():
    event = _FakeEvent([True])
    clock = iter([0.0, 0.0])
    refreshes: list[None] = []

    refreshed_manually = main._wait_with_display_refresh(
        event,
        interval_seconds=60,
        refresh_display=lambda: refreshes.append(None),
        clock=lambda: next(clock),
    )

    assert refreshed_manually is True
    assert refreshes == []


def test_wait_returns_without_refresh_when_shutdown_is_already_requested():
    shutdown_requested = threading.Event()
    shutdown_requested.set()
    refreshes: list[None] = []

    refreshed_manually = main._wait_with_display_refresh(
        threading.Event(),
        interval_seconds=60,
        refresh_display=lambda: refreshes.append(None),
        shutdown_requested=shutdown_requested,
    )

    assert refreshed_manually is False
    assert refreshes == []


def test_ctrl_c_requests_shutdown_wakes_poll_and_stops_tray_icon():
    shutdown_requested = threading.Event()
    manual_refresh = threading.Event()
    icon = _FakeIcon()

    handled = main._handle_console_control_event(
        main._CTRL_C_EVENT,
        shutdown_requested,
        manual_refresh,
        icon,
    )

    assert handled is True
    assert shutdown_requested.is_set()
    assert manual_refresh.is_set()
    assert icon.stop_calls == 1


def test_poll_interval_doubles_after_rate_limit():
    data = AnthropicUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 120


def test_poll_interval_backoff_is_capped():
    data = AnthropicUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
    )

    assert main._next_poll_interval_seconds(400, data, baseline_seconds=60) == 600
    assert main._next_poll_interval_seconds(600, data, baseline_seconds=60) == 600


def test_poll_interval_honors_retry_after_on_rate_limit():
    data = AnthropicUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=224,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 224


def test_poll_interval_retry_after_is_floored_at_baseline():
    data = AnthropicUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=30,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 60


def test_poll_interval_falls_back_to_backoff_without_retry_after():
    data = AnthropicUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=None,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 120


def test_poll_interval_decreases_by_five_seconds_after_success():
    data = AnthropicUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 85


def test_poll_interval_never_drops_below_baseline_after_success():
    data = AnthropicUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 60


def test_poll_interval_clamps_to_baseline_when_less_than_step_above_it():
    data = AnthropicUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(63, data, baseline_seconds=60) == 60


def test_poll_interval_stays_same_after_non_rate_limit_error():
    data = AnthropicUsageData(
        fetch_error="token_expired",
        fetched_at=NOW,
        status_code=401,
    )

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 90


def test_poll_interval_stays_same_after_offline_error_without_status():
    data = AnthropicUsageData(fetch_error="offline", fetched_at=NOW)

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 90
