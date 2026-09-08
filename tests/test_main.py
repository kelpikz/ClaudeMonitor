from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pathlib import Path

from claudemonitor import cli_refresher, codex_fetcher, main
from claudemonitor.config import (
    Config,
    PollingConfig,
    SessionRefreshConfig,
    ThresholdsConfig,
)
from claudemonitor.models import CLAUDE, CODEX, ProviderUsageData

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
        self.updates: list[tuple[list, str]] = []
        self.visible = visible

    def update(self, segments, tooltip: str) -> None:
        self.updates.append((segments, tooltip))

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


def _display_state(provider_key: str, taskbar_text: str, tooltip: str):
    return main.processor.DisplayState(
        provider_key=provider_key,
        icon_color="green",
        tooltip=tooltip,
        menu_status_label="updated",
        taskbar_text=taskbar_text,
        tray_text=taskbar_text,
    )


class TestApplyDisplay:
    """One icon carries every provider; the taskbar label carries them too."""

    def _run(self, monkeypatch, states):
        icon = _FakeIcon()
        companion = _FakeCompanion()
        applied: list[object] = []
        monkeypatch.setattr(
            main.tray,
            "apply",
            lambda target, value: applied.append((target, value)),
        )

        main._apply_display(icon, states, companion)

        return icon, companion, applied

    def test_one_provider_reaches_the_icon_and_the_label(self, monkeypatch):
        state = _display_state("claude", "80% (3h 0m)", "usage")

        icon, companion, applied = self._run(monkeypatch, [state])

        target, tray_state = applied[0]
        assert target is icon
        assert tray_state.tooltip == "Claude  80% (3h 0m)"
        segments, tooltip = companion.updates[0]
        assert [segment.text for segment in segments] == ["80% (3h 0m)"]
        assert tooltip == "usage"

    def test_both_providers_are_combined_onto_the_one_icon(self, monkeypatch):
        states = [
            _display_state("claude", "80%", "Claude usage"),
            _display_state("codex", "64%", "Codex usage"),
        ]

        _icon, _companion, applied = self._run(monkeypatch, states)

        assert len(applied) == 1
        _target, tray_state = applied[0]
        assert tray_state.tooltip == "Claude  80%\nCodex  64%"
        assert tray_state.status_lines == ["Claude — updated", "Codex — updated"]

    def test_the_taskbar_carries_both_providers(self, monkeypatch):
        states = [
            _display_state("claude", "80%", "Claude usage"),
            _display_state("codex", "64%", "Codex usage"),
        ]

        _icon, companion, _applied = self._run(monkeypatch, states)

        segments, tooltip = companion.updates[0]
        assert [(s.provider_key, s.text) for s in segments] == [
            ("claude", "80%"),
            ("codex", "64%"),
        ]
        assert tooltip == "Claude usage\n\nCodex usage"

    def test_codex_switched_off_mid_poll_leaves_claude_alone(self, monkeypatch):
        states = [_display_state("claude", "80%", "Claude usage")]

        _icon, companion, applied = self._run(monkeypatch, states)

        _target, tray_state = applied[0]
        assert tray_state.status_lines == ["Claude — updated"]
        assert companion.updates


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

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True
        assert manual_refresh.is_set()

    def test_disabling_the_config_section_disables_the_nudger(self):
        nudger, manual_refresh = self._nudger(enabled=False)

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is False
        assert not manual_refresh.is_set()

    def test_toggle_flips_the_nudger_and_persists_the_choice(self):
        nudger, _manual_refresh = self._nudger()
        saved: list[bool] = []

        main._toggle_session_refresh([nudger], saved.append)

        assert nudger.enabled is False
        assert saved == [False]

        main._toggle_session_refresh([nudger], saved.append)

        assert nudger.enabled is True
        assert saved == [False, True]

    def test_toggle_survives_a_failing_config_write(self, caplog):
        nudger, _manual_refresh = self._nudger()

        def unavailable(_enabled: bool) -> None:
            raise OSError("config is read-only")

        with caplog.at_level(logging.ERROR):
            main._toggle_session_refresh([nudger], unavailable)

        assert nudger.enabled is False
        assert "session refresh" in caplog.text

    def test_the_configured_cooldown_gates_the_second_attempt(self):
        nudger, _manual_refresh = self._nudger(cooldown_seconds=10_000)

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

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
    data = ProviderUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 120


def test_poll_interval_backoff_is_capped():
    data = ProviderUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
    )

    assert main._next_poll_interval_seconds(400, data, baseline_seconds=60) == 600
    assert main._next_poll_interval_seconds(600, data, baseline_seconds=60) == 600


def test_poll_interval_honors_retry_after_on_rate_limit():
    data = ProviderUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=224,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 224


def test_poll_interval_retry_after_is_floored_at_baseline():
    data = ProviderUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=30,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 60


def test_poll_interval_falls_back_to_backoff_without_retry_after():
    data = ProviderUsageData(
        fetch_error="rate_limited",
        fetched_at=NOW,
        status_code=429,
        retry_after_seconds=None,
    )

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 120


def test_poll_interval_decreases_by_five_seconds_after_success():
    data = ProviderUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 85


def test_poll_interval_never_drops_below_baseline_after_success():
    data = ProviderUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(60, data, baseline_seconds=60) == 60


def test_poll_interval_clamps_to_baseline_when_less_than_step_above_it():
    data = ProviderUsageData(fetched_at=NOW, status_code=200)

    assert main._next_poll_interval_seconds(63, data, baseline_seconds=60) == 60


def test_poll_interval_stays_same_after_non_rate_limit_error():
    data = ProviderUsageData(
        fetch_error="token_expired",
        fetched_at=NOW,
        status_code=401,
    )

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 90


def test_poll_interval_stays_same_after_offline_error_without_status():
    data = ProviderUsageData(fetch_error="offline", fetched_at=NOW)

    assert main._next_poll_interval_seconds(90, data, baseline_seconds=60) == 90


# ===========================================================================
# Wiring the second provider.
# ===========================================================================


class TestCodexToggle:
    """Turning Codex off must hide its icon and drop it from the taskbar."""

    def test_toggle_flips_the_setting_and_persists_it(self):
        saved: list[bool] = []
        tracker = main.CodexTracking(enabled=True)

        main._toggle_codex_tracking(tracker, saved.append)

        assert tracker.enabled is False
        assert saved == [False]

    def test_toggle_survives_a_failing_config_write(self, caplog):
        tracker = main.CodexTracking(enabled=True)

        def unavailable(_enabled: bool) -> None:
            raise OSError("config is read-only")

        with caplog.at_level(logging.ERROR):
            main._toggle_codex_tracking(tracker, unavailable)

        assert tracker.enabled is False
        assert "codex" in caplog.text.lower()

    def test_switching_it_off_wakes_the_poll_loop(self):
        # Codex used to vanish with its own tray icon the instant it was
        # switched off. With one icon, nothing changes until the loop polls
        # again — up to a minute of showing a provider nobody is tracking.
        woken = threading.Event()
        tracker = main.CodexTracking(enabled=True)

        main._toggle_codex_tracking(tracker, lambda _enabled: None, woken.set)

        assert woken.is_set()

    def test_the_loop_is_woken_even_when_the_config_write_fails(self):
        woken = threading.Event()
        tracker = main.CodexTracking(enabled=True)

        def unavailable(_enabled: bool) -> None:
            raise OSError("config is read-only")

        main._toggle_codex_tracking(tracker, unavailable, woken.set)

        assert woken.is_set()

    def test_switching_it_back_on_restores_tracking(self):
        tracker = main.CodexTracking(enabled=False)

        main._toggle_codex_tracking(tracker, lambda _enabled: None)

        assert tracker.enabled is True


class TestActiveProviders:
    """The poll loop only works on providers the user is actually tracking."""

    def test_both_providers_are_polled_when_codex_is_on(self):
        claude, codex = object(), object()

        assert main._active_pollers(claude, codex, codex_enabled=True) == [claude, codex]

    def test_codex_is_dropped_when_switched_off(self):
        claude, codex = object(), object()

        assert main._active_pollers(claude, codex, codex_enabled=False) == [claude]


class TestSharedPollInterval:
    """One loop serves both providers, so the slower of the two sets the pace."""

    def test_the_longer_interval_wins(self):
        assert main._shared_poll_interval([60, 240]) == 240

    def test_a_single_provider_sets_its_own_pace(self):
        assert main._shared_poll_interval([90]) == 90

    def test_no_providers_falls_back_to_a_safe_default(self):
        assert main._shared_poll_interval([]) > 0


class TestProviderFailureIsolation:
    """One provider misbehaving must never blank the other one's numbers."""

    class _Poller:
        def __init__(self, provider, *, raises: Exception | None = None):
            self.provider = provider
            self._raises = raises
            self.polls = 0

        def poll(self):
            self.polls += 1
            if self._raises is not None:
                raise self._raises
            return ["notification"]

        def display(self, now):
            if self._raises is not None:
                raise self._raises
            return main.processor.DisplayState(
                provider_key=self.provider.key,
                icon_color="green",
                tooltip="fine",
                menu_status_label="Updated 0s ago",
                taskbar_text="80%",
                tray_text="80%",
            )

    def test_a_failing_poll_returns_no_notifications(self, caplog):
        poller = self._Poller(CODEX, raises=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            assert main._poll_provider(poller) == []

        assert "Codex" in caplog.text

    def test_a_healthy_poll_passes_its_notifications_through(self):
        assert main._poll_provider(self._Poller(CLAUDE)) == ["notification"]

    def test_a_failing_display_still_yields_a_state_for_that_provider(self, caplog):
        poller = self._Poller(CODEX, raises=RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            state = main._provider_display(poller, NOW)

        assert state.provider_key == "codex"
        assert state.icon_color == "grey"

    def test_a_healthy_display_is_returned_unchanged(self):
        state = main._provider_display(self._Poller(CLAUDE), NOW)

        assert state.icon_color == "green"

    def test_one_broken_provider_leaves_the_other_intact(self):
        healthy = self._Poller(CLAUDE)
        broken = self._Poller(CODEX, raises=RuntimeError("boom"))

        states = [main._provider_display(p, NOW) for p in (healthy, broken)]

        assert [s.icon_color for s in states] == ["green", "grey"]


# ===========================================================================
# End to end: what the ChatGPT backend answers -> whether the Codex CLI runs.
# ===========================================================================


class _FakeCodexResponse:
    """Stand-in for httpx.Response covering only what codex_fetcher reads."""

    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json = json_body
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        """Nothing to raise: every body here is a 200."""


class _RecordingCli:
    """Stands in for cli_refresher._run_cli, keeping the argv it would run.

    Patching subprocess itself would not work: run_codex_cli captured
    subprocess.run as a default argument when the module was imported.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, *, executable_name, build_command, which, run) -> bool:
        self.commands.append(build_command(f"{executable_name}.CMD"))
        return True


def _codex_usage_body(used_percent: float, reset_at: int | None) -> dict:
    """Build a body shaped like GET /backend-api/wham/usage."""
    window = {
        "used_percent": used_percent,
        "limit_window_seconds": 18000,
        "reset_at": reset_at,
    }
    return {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": dict(window),
            "secondary_window": dict(window, limit_window_seconds=604800),
        },
    }


class TestIdleCodexWindowWakesTheCli:
    """An idle Codex window must run the Codex CLI, as an idle Claude one does.

    The path under test is the whole one — the bytes the ChatGPT backend
    returned, through the real fetcher and the real poller, to the argv handed
    to subprocess — because the defect lived in the join between those parts
    and no part was wrong on its own.
    """

    def _codex_poller(self, monkeypatch, body: dict):
        """Build the production Codex poller over a canned response body."""
        monkeypatch.setattr(
            codex_fetcher,
            "_read_credentials",
            lambda: codex_fetcher.CodexCredentials(
                access_token="test-token",
                account_id="test-account",
                expires_at=None,
            ),
        )
        monkeypatch.setattr(
            codex_fetcher.httpx,
            "get",
            lambda *args, **kwargs: _FakeCodexResponse(200, body),
        )
        cli = _RecordingCli()
        monkeypatch.setattr(cli_refresher, "_run_cli", cli)

        config = Config()
        nudger = main.create_codex_nudger(
            config,
            threading.Event(),
            start_background=lambda work: work(),
        )
        return main.ProviderPoller(CODEX, codex_fetcher.fetch, config, nudger), cli

    def _poll(self, monkeypatch, body: dict) -> list[list[str]]:
        """Run one production Codex poll over a canned body; return the argv run."""
        poller, cli = self._codex_poller(monkeypatch, body)
        poller.poll()
        return cli.commands

    def test_an_untouched_window_runs_the_codex_cli(self, monkeypatch):
        commands = self._poll(monkeypatch, _codex_usage_body(0.0, None))

        assert len(commands) == 1
        assert commands[0][0] == "codex.CMD"
        assert commands[0][1] == "exec"
        assert commands[0][-1] == "hi"

    def test_a_window_reported_as_zero_percent_used_runs_the_cli(self, monkeypatch):
        # Codex reports a reset time even for a window nothing was sent to, so
        # "0% used, with a countdown" is how the idle state usually arrives.
        commands = self._poll(monkeypatch, _codex_usage_body(0.0, 1787860125))

        assert len(commands) == 1

    def test_a_live_window_is_left_alone(self, monkeypatch):
        commands = self._poll(monkeypatch, _codex_usage_body(57.0, 1787860125))

        assert commands == []

    def test_a_missing_window_is_left_alone(self, monkeypatch):
        # No primary window at all is a shape we cannot read; spending a
        # message to find out what it meant is not worth the quota.
        body = _codex_usage_body(0.0, None)
        body["rate_limit"]["primary_window"] = None

        assert self._poll(monkeypatch, body) == []

    def test_the_second_poll_is_held_off_by_the_cooldown(self, monkeypatch):
        # An idle window stays idle until the reply lands, so without the
        # cooldown every poll would start another Codex turn.
        poller, cli = self._codex_poller(monkeypatch, _codex_usage_body(0.0, None))

        poller.poll()
        poller.poll()

        assert len(cli.commands) == 1


# ===========================================================================
# The settings window the tray menu now opens.
# ===========================================================================


class _SettingsCompanion(_FakeCompanion):
    """A taskbar companion that also reports whether it is working."""

    healthy = True


class TestSettingsWiring:
    """Every switch that left the tray menu still reaches the same code, and the
    numbers that used to be config-file-only now reach the running app too."""

    def _model(self, **overrides):
        fields = {
            "companion": _SettingsCompanion(visible=True),
            "codex_tracking": main.CodexTracking(enabled=True),
            "nudgers": [main.cli_refresher.SessionNudger(enabled=True)],
            "pollers": [_SettingsPoller(60), _SettingsPoller(60)],
            "config": Config(),
            "log_dir": Path("C:/logs"),
        }
        fields.update(overrides)
        return main.build_settings_model(**fields), fields

    def _field(self, model, key: str):
        return next(field for field in model.fields() if field.key == key)

    def test_the_taskbar_switch_reads_and_writes_the_companion(self, monkeypatch):
        monkeypatch.setattr(main, "save_taskbar_enabled", lambda _enabled: None)
        model, fields = self._model()
        taskbar = self._field(model, "taskbar")

        assert taskbar.is_on() is True
        taskbar.write(False)

        assert fields["companion"].visible is False

    def test_the_codex_switch_reads_and_writes_tracking(self, monkeypatch):
        monkeypatch.setattr(main, "save_codex_enabled", lambda _enabled: None)
        model, fields = self._model()
        codex = self._field(model, "codex")

        assert codex.is_on() is True
        codex.write(False)

        assert fields["codex_tracking"].enabled is False

    def test_the_codex_switch_asks_for_a_fresh_poll(self, monkeypatch):
        monkeypatch.setattr(main, "save_codex_enabled", lambda _enabled: None)
        woken = threading.Event()
        model, _fields = self._model(wake_poll_loop=woken.set)

        self._field(model, "codex").write(False)

        assert woken.is_set()

    def test_the_refresh_switch_reads_and_writes_every_nudger(self, monkeypatch):
        monkeypatch.setattr(main, "save_session_refresh_enabled", lambda _enabled: None)
        model, fields = self._model()
        refresh = self._field(model, "session_refresh")

        assert refresh.is_on() is True
        refresh.write(False)

        assert all(not nudger.enabled for nudger in fields["nudgers"])

    def test_the_startup_switch_reads_the_registry(self, monkeypatch):
        monkeypatch.setattr(main.autostart, "is_enabled", lambda: True)
        model, _fields = self._model()

        assert self._field(model, "startup").is_on() is True

    def test_the_startup_switch_writes_the_registry(self, monkeypatch):
        saved: list[bool] = []
        monkeypatch.setattr(main.autostart, "set_enabled", lambda enabled: saved.append(enabled))
        model, _fields = self._model()

        self._field(model, "startup").write(True)

        assert saved == [True]

    def test_an_unavailable_taskbar_label_disables_its_switch(self):
        companion = _SettingsCompanion(visible=True)
        companion.healthy = False
        model, _fields = self._model(companion=companion)

        assert self._field(model, "taskbar").available() is False

    def test_the_log_link_points_at_the_log_directory(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(main.os, "startfile", opened.append)
        model, _fields = self._model()

        model.links()[0].open()

        assert opened == [str(Path("C:/logs"))]


class TestNumericSettingsWiring:
    """A number written in the window has to change the app that is running,
    not only the file it will read at the next launch."""

    def _model(self, **overrides):
        fields = {
            "companion": _SettingsCompanion(visible=True),
            "codex_tracking": main.CodexTracking(enabled=True),
            "nudgers": [main.cli_refresher.SessionNudger(enabled=True)],
            "pollers": [_SettingsPoller(60), _SettingsPoller(60)],
            "config": Config(),
            "log_dir": Path("C:/logs"),
        }
        fields.update(overrides)
        return main.build_settings_model(**fields), fields

    def _field(self, model, key: str):
        return next(field for field in model.fields() if field.key == key)

    def test_the_poll_interval_reads_the_config(self):
        config = Config(polling=PollingConfig(interval_seconds=45))
        model, _fields = self._model(config=config)

        assert self._field(model, "poll_interval").value() == 45

    def test_writing_the_poll_interval_updates_the_running_config(self, monkeypatch):
        monkeypatch.setattr(main, "save_poll_interval_seconds", lambda _seconds: None)
        model, fields = self._model()

        self._field(model, "poll_interval").write(120)

        assert fields["config"].polling.interval_seconds == 120

    def test_writing_the_poll_interval_resets_every_poller(self, monkeypatch):
        # A poller that has backed off keeps its own longer interval; leaving
        # it would ignore the new setting for as long as the backoff lasts.
        monkeypatch.setattr(main, "save_poll_interval_seconds", lambda _seconds: None)
        model, fields = self._model()
        fields["pollers"][0].interval_seconds = 480

        self._field(model, "poll_interval").write(120)

        assert [poller.interval_seconds for poller in fields["pollers"]] == [120, 120]

    def test_writing_the_poll_interval_persists_it(self, monkeypatch):
        saved: list[int] = []
        monkeypatch.setattr(main, "save_poll_interval_seconds", saved.append)
        model, _fields = self._model()

        self._field(model, "poll_interval").write(120)

        assert saved == [120]

    def test_writing_the_poll_interval_wakes_the_loop(self, monkeypatch):
        monkeypatch.setattr(main, "save_poll_interval_seconds", lambda _seconds: None)
        woken = threading.Event()
        model, _fields = self._model(wake_poll_loop=woken.set)

        self._field(model, "poll_interval").write(120)

        assert woken.is_set()

    def test_the_thresholds_read_the_config(self):
        config = Config(thresholds=ThresholdsConfig(amber_below=40, red_below=15))
        model, _fields = self._model(config=config)

        assert self._field(model, "amber_threshold").value() == 40
        assert self._field(model, "red_threshold").value() == 15

    def test_writing_a_threshold_updates_the_running_config(self, monkeypatch):
        monkeypatch.setattr(main, "save_amber_threshold", lambda _percent: None)
        monkeypatch.setattr(main, "save_red_threshold", lambda _percent: None)
        model, fields = self._model()

        self._field(model, "amber_threshold").write(40)
        self._field(model, "red_threshold").write(15)

        assert fields["config"].thresholds.amber_below == 40
        assert fields["config"].thresholds.red_below == 15

    def test_the_cooldown_reads_the_config(self):
        config = Config(session_refresh=SessionRefreshConfig(cooldown_seconds=300))
        model, _fields = self._model(config=config)

        assert self._field(model, "refresh_cooldown").value() == 300

    def test_writing_the_cooldown_reaches_every_nudger(self, monkeypatch):
        monkeypatch.setattr(main, "save_session_refresh_cooldown", lambda _seconds: None)
        nudger = main.cli_refresher.SessionNudger(enabled=True, cooldown_seconds=900)
        model, _fields = self._model(nudgers=[nudger])

        self._field(model, "refresh_cooldown").write(300)

        assert nudger._cooldown_seconds == 300

    def test_a_failing_config_write_still_changes_the_running_app(self, monkeypatch, caplog):
        # These run on the settings window's own thread, where an escaping
        # error would be an invisible traceback and a dialog that half-worked.
        def unwritable(_value):
            raise OSError("config is read-only")

        monkeypatch.setattr(main, "save_poll_interval_seconds", unwritable)
        model, fields = self._model()

        with caplog.at_level(logging.ERROR):
            self._field(model, "poll_interval").write(120)

        assert fields["config"].polling.interval_seconds == 120
        assert "poll interval" in caplog.text


class TestSettingsControllerWiring:
    """The tray hands off to a controller that owns the one window."""

    def _model(self):
        return main.build_settings_model(
            companion=_SettingsCompanion(),
            codex_tracking=main.CodexTracking(enabled=True),
            nudgers=[],
            pollers=[],
            config=Config(),
            log_dir=Path("."),
        )

    def test_the_controller_builds_a_window_from_the_model(self):
        built: list[object] = []
        controller = main.create_settings_controller(
            self._model(),
            build_window=lambda model: built.append(model) or _NullWindow(),
            start_background=lambda work: work(),
        )

        controller.open()

        assert len(built) == 1

    def test_a_window_that_cannot_be_built_does_not_reach_the_tray(self, caplog):
        # This runs inside pystray's message loop, where an escaping error is
        # an invisible stderr traceback in a windowed build.
        def unavailable(_model):
            raise OSError("no window station")

        controller = main.create_settings_controller(
            self._model(),
            build_window=unavailable,
            start_background=lambda work: work(),
        )

        with caplog.at_level(logging.ERROR):
            controller.open()

        assert "settings window" in caplog.text


class _SettingsPoller:
    """Stands in for a ProviderPoller: only its live interval matters here."""

    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = interval_seconds


class _NullWindow:
    """A settings window that opens and closes without doing anything."""

    def show(self) -> None:
        """Return at once, as a window the user closed immediately would."""

    def focus(self) -> bool:
        return True

    def close(self) -> None:
        """Nothing to close."""
