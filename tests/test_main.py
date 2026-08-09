from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from claudemonitor import main
from claudemonitor.config import Config, SessionRefreshConfig
from claudemonitor.models import AnthropicUsageData

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

    def update(self, text: str, tooltip: str) -> None:
        self.updates.append((text, tooltip))

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class TestToggleTaskbarVisibility:
    """Toggling runs inside pystray's message loop, where an escaping exception
    would surface only as an invisible stderr traceback in a windowed build."""

    def test_toggle_flips_visibility_and_persists_the_choice(self):
        companion = _FakeCompanion(visible=True)
        saved: list[bool] = []

        main._toggle_taskbar_visibility(companion, saved.append)

        assert companion.visible is False
        assert saved == [False]

    def test_toggle_survives_a_failing_config_write(self, caplog):
        companion = _FakeCompanion(visible=False)

        def unwritable(_visible: bool) -> None:
            raise OSError("config file is locked")

        with caplog.at_level(logging.ERROR):
            main._toggle_taskbar_visibility(companion, unwritable)

        assert companion.visible is True
        assert "taskbar visibility" in caplog.text


class TestToggleStartupRegistration:
    def test_toggle_enables_startup_when_currently_disabled(self):
        saved: list[bool] = []

        main._toggle_startup_registration(lambda: False, saved.append)

        assert saved == [True]

    def test_toggle_disables_startup_when_currently_enabled(self):
        saved: list[bool] = []

        main._toggle_startup_registration(lambda: True, saved.append)

        assert saved == [False]

    def test_toggle_survives_a_registry_failure(self, caplog):
        def unavailable() -> bool:
            raise OSError("registry unavailable")

        with caplog.at_level(logging.ERROR):
            main._toggle_startup_registration(unavailable, lambda enabled: None)

        assert "Windows startup registration" in caplog.text


def test_startup_state_falls_back_to_unchecked_when_registry_read_fails(caplog):
    def unavailable() -> bool:
        raise OSError("registry unavailable")

    with caplog.at_level(logging.ERROR):
        enabled = main._startup_registration_enabled(unavailable)

    assert enabled is False
    assert "Windows startup registration" in caplog.text


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


def test_apply_display_updates_tray_and_taskbar(monkeypatch):
    icon = _FakeIcon()
    companion = _FakeCompanion()
    state = main.processor.DisplayState(
        icon_color="green",
        tooltip="usage",
        menu_status_label="updated",
        taskbar_text="80% (3h 0m)",
    )
    applied: list[object] = []
    monkeypatch.setattr(main.tray, "apply", lambda target, value: applied.append((target, value)))

    main._apply_display(icon, state, companion)

    assert applied == [(icon, state)]
    assert companion.updates == [("80% (3h 0m)", "usage")]


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

    def test_toggle_flips_the_nudger_and_persists_the_choice(self):
        nudger, _manual_refresh = self._nudger()
        saved: list[bool] = []

        main._toggle_session_refresh(nudger, saved.append)

        assert nudger.enabled is False
        assert saved == [False]

        main._toggle_session_refresh(nudger, saved.append)

        assert nudger.enabled is True
        assert saved == [False, True]

    def test_toggle_survives_a_failing_config_write(self, caplog):
        nudger, _manual_refresh = self._nudger()

        def unavailable(_enabled: bool) -> None:
            raise OSError("config is read-only")

        with caplog.at_level(logging.ERROR):
            main._toggle_session_refresh(nudger, unavailable)

        assert nudger.enabled is False
        assert "session refresh" in caplog.text

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
