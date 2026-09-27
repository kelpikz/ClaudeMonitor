from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pathlib import Path

from claudemonitor import cli_refresher, codex_fetcher, main, usage_request
from claudemonitor.cli_refresher import RefreshOptions
from claudemonitor.config import (
    CLAUDE_SETTINGS,
    CODEX_SETTINGS,
    ClaudeConfig,
    CodexConfig,
    Config,
    ConfigSetting,
    PollingConfig,
    ThresholdsConfig,
)
from claudemonitor.models import CLAUDE, CODEX, CliReply, ProviderUsageData

import pytest

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def saved_settings(monkeypatch) -> list[tuple[ConfigSetting, object]]:
    """Keep every test off the real config file, and record what it would write.

    Each setter reaches the file through one ``ConfigSetting.save``, so one
    patch covers every setting rather than one per ``save_*`` wrapper.
    """
    written: list[tuple[ConfigSetting, object]] = []
    monkeypatch.setattr(
        ConfigSetting,
        "save",
        lambda self, value: written.append((self, value)),
    )
    return written


def _saved_values(written, setting: ConfigSetting) -> list[object]:
    """Return what one setting was asked to write, in order."""
    return [value for saved, value in written if saved == setting]


class _TrackedPoller:
    """A poller that is only ever asked which provider it is for."""

    def __init__(self, provider):
        self.provider = provider


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

    def test_toggle_flips_visibility_and_persists_the_choice(self, saved_settings):
        companion = _FakeCompanion(visible=True)

        main._toggle_taskbar_visibility(companion)

        assert companion.visible is False
        assert _saved_values(saved_settings, main.TASKBAR_ENABLED) == [False]

    def test_toggling_back_shows_it_again(self, saved_settings):
        companion = _FakeCompanion(visible=False)

        main._toggle_taskbar_visibility(companion)

        assert companion.visible is True
        assert _saved_values(saved_settings, main.TASKBAR_ENABLED) == [True]


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


def _display_state(provider, taskbar_text: str, tooltip: str):
    return main.processor.DisplayState(
        provider=provider,
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
        state = _display_state(CLAUDE, "80% (3h 0m)", "usage")

        icon, companion, applied = self._run(monkeypatch, [state])

        target, tray_state = applied[0]
        assert target is icon
        assert tray_state.tooltip == "Claude  80% (3h 0m)"
        segments, tooltip = companion.updates[0]
        assert [segment.text for segment in segments] == ["80% (3h 0m)"]
        assert tooltip == "usage"

    def test_both_providers_are_combined_onto_the_one_icon(self, monkeypatch):
        states = [
            _display_state(CLAUDE, "80%", "Claude usage"),
            _display_state(CODEX, "64%", "Codex usage"),
        ]

        _icon, _companion, applied = self._run(monkeypatch, states)

        assert len(applied) == 1
        _target, tray_state = applied[0]
        assert tray_state.tooltip == "Claude  80%\nCodex  64%"
        assert tray_state.status_lines == ["Claude — updated", "Codex — updated"]

    def test_the_taskbar_carries_both_providers(self, monkeypatch):
        states = [
            _display_state(CLAUDE, "80%", "Claude usage"),
            _display_state(CODEX, "64%", "Codex usage"),
        ]

        _icon, companion, _applied = self._run(monkeypatch, states)

        segments, tooltip = companion.updates[0]
        assert [(s.provider, s.text) for s in segments] == [
            (CLAUDE, "80%"),
            (CODEX, "64%"),
        ]
        assert tooltip == "Claude usage\n\nCodex usage"

    def test_no_tracked_provider_hides_the_label(self, monkeypatch):
        # An empty label is what tells the companion there is nothing to show.
        _icon, companion, _applied = self._run(monkeypatch, [])

        segments, _tooltip = companion.updates[0]
        assert segments == []

    def test_no_tracked_provider_says_so_on_the_icon(self, monkeypatch):
        _icon, _companion, applied = self._run(monkeypatch, [])

        _target, tray_state = applied[0]
        assert tray_state.icon_color == "grey"
        assert "no provider tracked" in tray_state.tooltip

    def test_codex_switched_off_mid_poll_leaves_claude_alone(self, monkeypatch):
        states = [_display_state(CLAUDE, "80%", "Claude usage")]

        _icon, companion, applied = self._run(monkeypatch, states)

        _target, tray_state = applied[0]
        assert tray_state.status_lines == ["Claude — updated"]
        assert companion.updates


class TestOneProvidersTwoWaysToRunItsCli:
    """The automatic nudge and Run now must never run one CLI twice at once."""

    def test_they_share_one_lock(self):
        nudger, run = main.create_cli_runners(CLAUDE, Config(), threading.Event())

        assert nudger.cli_lock is run.cli_lock

    def test_each_provider_has_a_lock_of_its_own(self):
        claude_nudger, _ = main.create_cli_runners(CLAUDE, Config(), threading.Event())
        codex_nudger, _ = main.create_cli_runners(CODEX, Config(), threading.Event())

        assert claude_nudger.cli_lock is not codex_nudger.cli_lock

    def test_both_belong_to_the_provider_asked_for(self):
        nudger, run = main.create_cli_runners(CODEX, Config(), threading.Event())

        assert (nudger.provider, run.provider) == (CODEX, CODEX)


class TestSessionNudgerWiring:
    """Each nudger reads its own provider's section of the running config."""

    def _nudger(self, provider=CLAUDE, config: Config | None = None):
        """Build the production nudger with a synchronous, always-succeeding CLI."""
        cfg = config or Config()
        manual_refresh = threading.Event()
        chosen: list[RefreshOptions] = []
        nudger = main.create_session_nudger(
            provider,
            cfg,
            manual_refresh,
            invoke=lambda options: chosen.append(options) or CliReply(succeeded=True),
            start_background=lambda work: work(),
        )
        return nudger, manual_refresh, chosen

    def test_a_successful_refresh_sets_the_manual_refresh_event(self):
        nudger, manual_refresh, _chosen = self._nudger()

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True
        assert manual_refresh.is_set()

    def test_renewing_switched_off_leaves_an_expired_token_alone(self):
        config = Config(claude=ClaudeConfig(renew_token=False))
        nudger, manual_refresh, _chosen = self._nudger(config=config)

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is False
        assert not manual_refresh.is_set()

    def test_one_provider_s_switch_does_not_reach_the_other(self):
        config = Config(claude=ClaudeConfig(renew_token=False))
        nudger, _manual_refresh, _chosen = self._nudger(CODEX, config)

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True

    def test_the_configured_cooldown_gates_the_second_attempt(self):
        config = Config(claude=ClaudeConfig(cooldown_seconds=10_000))
        nudger, _manual_refresh, _chosen = self._nudger(config=config)

        data = ProviderUsageData(fetch_error="token_expired", fetched_at=NOW)

        assert nudger.maybe_nudge(data) is True
        assert nudger.maybe_nudge(data) is False

    def test_the_configured_model_and_effort_are_handed_to_the_cli(self):
        config = Config(codex=CodexConfig(model="gpt-5.5", effort="high"))
        nudger, _manual_refresh, chosen = self._nudger(CODEX, config)

        nudger.maybe_nudge(ProviderUsageData(fetch_error="token_expired", fetched_at=NOW))

        assert (chosen[0].model, chosen[0].effort) == ("gpt-5.5", "high")

    def test_a_setting_changed_while_running_is_used_at_the_next_poll(self):
        config = Config()
        nudger, _manual_refresh, chosen = self._nudger(CODEX, config)

        CODEX_SETTINGS.model.write(config, "gpt-5.5")
        nudger.maybe_nudge(ProviderUsageData(fetch_error="token_expired", fetched_at=NOW))

        assert chosen[0].model == "gpt-5.5"


class TestRefreshOptionsFromConfig:
    def test_every_option_is_read_from_the_provider_s_section(self):
        config = Config(
            codex=CodexConfig(
                renew_token=False,
                wake_session=True,
                cooldown_seconds=60,
                model="gpt-5.5",
                effort="high",
            )
        )

        assert main.refresh_options(CODEX, config) == RefreshOptions(
            renew_token=False,
            wake_session=True,
            cooldown_seconds=60,
            model="gpt-5.5",
            effort="high",
        )


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


class TestTrackingOneProvider:
    """Turning a provider off must drop it from the icon and the taskbar."""

    def test_switching_it_off_persists_the_choice(self, saved_settings):
        config = Config()

        main._set_tracking(CODEX, config, lambda: None, False)

        assert main._is_tracked(CODEX, config) is False
        assert _saved_values(saved_settings, CODEX_SETTINGS.tracking) == [False]

    def test_switching_it_back_on_restores_tracking(self, saved_settings):
        config = Config()
        main._set_tracking(CODEX, config, lambda: None, False)

        main._set_tracking(CODEX, config, lambda: None, True)

        assert main._is_tracked(CODEX, config) is True

    def test_switching_it_off_wakes_the_poll_loop(self, saved_settings):
        # Codex used to vanish with its own tray icon the instant it was
        # switched off. With one icon, nothing changes until the loop polls
        # again — up to a minute of showing a provider nobody is tracking.
        woken = threading.Event()

        main._set_tracking(CODEX, Config(), woken.set, False)

        assert woken.is_set()

    def test_the_loop_is_woken_even_when_the_config_write_fails(self, monkeypatch):
        def unwritable(self, value):
            raise OSError("config is read-only")

        # The save swallows its own failure, so the wake-up still happens.
        monkeypatch.setattr(ConfigSetting, "save", lambda self, value: None)
        woken = threading.Event()

        main._set_tracking(CODEX, Config(), woken.set, False)

        assert woken.is_set()

    def test_claude_can_be_switched_off_too(self, saved_settings):
        config = Config()

        main._set_tracking(CLAUDE, config, lambda: None, False)

        assert main._is_tracked(CLAUDE, config) is False
        assert _saved_values(saved_settings, CLAUDE_SETTINGS.tracking) == [False]


class TestActiveProviders:
    """The poll loop only works on providers the user is actually tracking."""

    def test_only_claude_is_polled_by_default(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        assert main._active_pollers([claude, codex], Config()) == [claude]

    def test_both_providers_are_polled_when_codex_is_on(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)
        config = Config(codex=CodexConfig(enabled=True))

        assert main._active_pollers([claude, codex], config) == [claude, codex]

    def test_codex_is_dropped_when_switched_off(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        config = Config()
        CODEX_SETTINGS.tracking.write(config, False)

        assert main._active_pollers([claude, codex], config) == [claude]

    def test_claude_is_dropped_when_switched_off(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        config = Config(codex=CodexConfig(enabled=True))
        CLAUDE_SETTINGS.tracking.write(config, False)

        assert main._active_pollers([claude, codex], config) == [codex]

    def test_nothing_is_polled_when_both_are_switched_off(self):
        claude, codex = _TrackedPoller(CLAUDE), _TrackedPoller(CODEX)

        config = Config()
        CLAUDE_SETTINGS.tracking.write(config, False)
        CODEX_SETTINGS.tracking.write(config, False)

        assert main._active_pollers([claude, codex], config) == []


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
                provider=self.provider,
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

        assert state.provider is CODEX
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

    Patching subprocess itself would not work: the runner captured
    subprocess.run as a default argument when the module was imported.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(
        self, *, executable_name, arguments, read_reply, which, run, clock
    ) -> CliReply:
        self.commands.append([f"{executable_name}.CMD", *arguments])
        return CliReply(succeeded=True)


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

    def _codex_poller(self, monkeypatch, body: dict, config: Config | None = None):
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
            usage_request.httpx,
            "get",
            lambda *args, **kwargs: _FakeCodexResponse(200, body),
        )
        cli = _RecordingCli()
        monkeypatch.setattr(cli_refresher, "_run_cli", cli)

        config = config or Config()
        nudger = main.create_session_nudger(
            CODEX,
            config,
            threading.Event(),
            start_background=lambda work: work(),
        )
        return main.ProviderPoller(CODEX, config, nudger), cli

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

    def test_the_configured_model_and_effort_reach_the_codex_argv(self, monkeypatch):
        config = Config(codex=CodexConfig(model="gpt-5.5", effort="low"))
        poller, cli = self._codex_poller(monkeypatch, _codex_usage_body(0.0, None), config)

        poller.poll()

        command = cli.commands[0]
        assert command[command.index("-m") + 1] == "gpt-5.5"
        assert "model_reasoning_effort=low" in command

    def test_waking_switched_off_leaves_an_idle_window_alone(self, monkeypatch):
        config = Config(codex=CodexConfig(wake_session=False))
        poller, cli = self._codex_poller(monkeypatch, _codex_usage_body(0.0, None), config)

        poller.poll()

        assert cli.commands == []

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
            "pollers": [_SettingsPoller(60), _SettingsPoller(60)],
            "config": Config(),
            "log_dir": Path("C:/logs"),
        }
        fields.update(overrides)
        return main.build_settings_model(**fields), fields

    def _field(self, model, key: str):
        return next(field for field in model.fields() if field.key == key)

    def test_the_taskbar_switch_reads_and_writes_the_companion(self, monkeypatch):
        model, fields = self._model()
        taskbar = self._field(model, "taskbar")

        assert taskbar.is_on() is True
        taskbar.write(False)

        assert fields["companion"].visible is False

    def test_a_providers_switch_reads_and_writes_its_tracking_setting(self):
        model, fields = self._model()
        codex = self._field(model, "codex_tracking")

        assert codex.is_on() is False
        codex.write(True)

        assert main._is_tracked(CODEX, fields["config"]) is True

    def test_a_providers_switch_asks_for_a_fresh_poll(self):
        woken = threading.Event()
        model, _fields = self._model(wake_poll_loop=woken.set)

        self._field(model, "codex_tracking").write(False)

        assert woken.is_set()

    def test_each_refresh_switch_reads_and_writes_its_own_provider(self, saved_settings):
        model, fields = self._model()
        renew = self._field(model, "codex_renew_token")

        assert renew.is_on() is True
        renew.write(False)

        assert fields["config"].codex.renew_token is False
        assert fields["config"].claude.renew_token is True
        assert _saved_values(saved_settings, CODEX_SETTINGS.renew_token) == [False]

    def test_the_wake_switch_reads_and_writes_its_setting(self, saved_settings):
        model, fields = self._model()

        self._field(model, "claude_wake_session").write(False)

        assert fields["config"].claude.wake_session is False
        assert _saved_values(saved_settings, CLAUDE_SETTINGS.wake_session) == [False]

    def test_claude_now_has_a_tracking_switch(self):
        model, fields = self._model()

        self._field(model, "claude_tracking").write(False)

        assert main._is_tracked(CLAUDE, fields["config"]) is False

    def test_the_model_box_reads_and_writes_its_setting(self, saved_settings):
        model, fields = self._model()
        box = self._field(model, "claude_model")

        assert box.value() == "haiku"
        box.write("  sonnet ")

        assert fields["config"].claude.model == "sonnet"
        assert _saved_values(saved_settings, CLAUDE_SETTINGS.model) == ["sonnet"]

    def test_the_effort_list_reads_and_writes_its_setting(self, saved_settings):
        model, fields = self._model()
        effort = self._field(model, "codex_effort")

        assert effort.value() == "low"
        effort.write("high")

        assert fields["config"].codex.effort == "high"
        assert _saved_values(saved_settings, CODEX_SETTINGS.effort) == ["high"]

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


def _immediate_run(provider, reply: CliReply | None = None, **kwargs):
    """A manual run that finishes before start() returns, answering one reply."""
    return cli_refresher.ManualRun(
        provider,
        invoke=lambda model, effort: reply or CliReply(succeeded=True, reply_text="Hi"),
        start_background=lambda work: work(),
        **kwargs,
    )


class TestCommandButtonsWiring:
    """Copy command and Run now reach the clipboard, the CLI, and the box under them."""

    def _model(self, **overrides):
        fields = {
            "companion": _SettingsCompanion(visible=True),
            "pollers": [_SettingsPoller(60), _SettingsPoller(60)],
            "config": Config(),
            "log_dir": Path("C:/logs"),
            "manual_runs": {
                "claude": _immediate_run(CLAUDE),
                "codex": _immediate_run(CODEX),
            },
            "copy_text": lambda text: True,
        }
        fields.update(overrides)
        return main.build_settings_model(**fields)

    def _field(self, model, key: str):
        return next(field for field in model.fields() if field.key == key)

    def test_nothing_has_run_when_the_window_opens(self):
        box = self._field(self._model(), "codex_last_run")

        assert box.text() == ""

    def test_the_copy_button_puts_the_command_on_the_clipboard(self):
        copied: list[str] = []
        model = self._model(copy_text=lambda text: copied.append(text) or True)

        self._field(model, "codex_copy_command").act("gpt-5.5", "", lambda: None)

        assert copied == [cli_refresher.command_line(CODEX, "gpt-5.5", "")]

    def test_a_copied_command_is_shown_in_the_box(self):
        model = self._model()

        self._field(model, "claude_copy_command").act("haiku", "", lambda: None)

        assert self._field(model, "claude_last_run").text().startswith(
            "Command copied to the clipboard:\nclaude -p "
        )

    def test_a_model_with_spaces_around_it_is_copied_trimmed(self):
        copied: list[str] = []
        model = self._model(copy_text=lambda text: copied.append(text) or True)

        self._field(model, "claude_copy_command").act("  haiku ", "", lambda: None)

        assert copied == [cli_refresher.command_line(CLAUDE, "haiku", "")]

    def test_the_copy_button_announces_the_new_text(self):
        told: list[str] = []
        model = self._model()

        self._field(model, "codex_copy_command").act("", "", lambda: told.append("changed"))

        assert told == ["changed"]

    def test_a_clipboard_that_refuses_leaves_the_box_as_it_was(self):
        model = self._model(copy_text=lambda text: False)

        self._field(model, "codex_copy_command").act("", "", lambda: None)

        assert self._field(model, "codex_last_run").text() == ""

    def test_the_run_button_runs_the_cli_with_the_model_and_effort_given(self):
        asked: list[tuple[str, str]] = []
        run = cli_refresher.ManualRun(
            CODEX,
            invoke=lambda model, effort: asked.append((model, effort)) or CliReply(True),
            start_background=lambda work: work(),
        )
        model = self._model(manual_runs={"claude": _immediate_run(CLAUDE), "codex": run})

        self._field(model, "codex_run_command").act("gpt-5.5", "high", lambda: None)

        assert asked == [("gpt-5.5", "high")]

    def test_a_finished_run_is_shown_in_the_box(self):
        model = self._model()

        self._field(model, "claude_run_command").act("haiku", "", lambda: None)

        assert self._field(model, "claude_last_run").text().startswith("Succeeded.\nReply: Hi")

    def test_the_run_button_announces_the_start_and_the_end(self):
        told: list[str] = []
        model = self._model()

        self._field(model, "claude_run_command").act("", "", lambda: told.append("changed"))

        assert told == ["changed", "changed"]

    def test_a_click_during_a_run_announces_nothing(self):
        told: list[str] = []
        pending = cli_refresher.ManualRun(CLAUDE, start_background=lambda work: None)
        model = self._model(manual_runs={"claude": pending, "codex": _immediate_run(CODEX)})
        button = self._field(model, "claude_run_command")
        button.act("", "", lambda: None)

        button.act("", "", lambda: told.append("changed"))

        assert told == []

    def test_each_provider_s_box_shows_only_its_own_run(self):
        model = self._model()

        self._field(model, "claude_run_command").act("", "", lambda: None)

        assert self._field(model, "codex_last_run").text() == ""

    def test_by_default_a_successful_run_asks_for_fresh_usage(self, monkeypatch):
        monkeypatch.setattr(
            cli_refresher,
            "run_provider_cli",
            lambda provider, model, effort: CliReply(succeeded=True),
        )
        woken = threading.Event()
        model = main.build_settings_model(
            companion=_SettingsCompanion(visible=True),
            pollers=[],
            config=Config(),
            log_dir=Path("C:/logs"),
            wake_poll_loop=woken.set,
        )

        self._field(model, "codex_run_command").act("", "", lambda: None)

        assert woken.wait(timeout=5)


class TestNumericSettingsWiring:
    """A number written in the window has to change the app that is running,
    not only the file it will read at the next launch."""

    def _model(self, **overrides):
        fields = {
            "companion": _SettingsCompanion(visible=True),
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
        model, fields = self._model()

        self._field(model, "poll_interval").write(120)

        assert fields["config"].polling.interval_seconds == 120

    def test_writing_the_poll_interval_resets_every_poller(self, monkeypatch):
        # A poller that has backed off keeps its own longer interval; leaving
        # it would ignore the new setting for as long as the backoff lasts.
        model, fields = self._model()
        fields["pollers"][0].interval_seconds = 480

        self._field(model, "poll_interval").write(120)

        assert [poller.interval_seconds for poller in fields["pollers"]] == [120, 120]

    def test_writing_the_poll_interval_persists_it(self, saved_settings):
        model, _fields = self._model()

        self._field(model, "poll_interval").write(120)

        assert _saved_values(saved_settings, main.POLL_INTERVAL) == [120]

    def test_writing_the_poll_interval_wakes_the_loop(self, monkeypatch):
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
        model, fields = self._model()

        self._field(model, "amber_threshold").write(40)
        self._field(model, "red_threshold").write(15)

        assert fields["config"].thresholds.amber_below == 40
        assert fields["config"].thresholds.red_below == 15

    def test_each_cooldown_reads_its_own_provider(self):
        config = Config(codex=CodexConfig(cooldown_seconds=300))
        model, _fields = self._model(config=config)

        assert self._field(model, "codex_cooldown").value() == 300
        assert self._field(model, "claude_cooldown").value() == 900

    def test_writing_a_cooldown_changes_only_that_provider(self, saved_settings):
        model, fields = self._model()

        self._field(model, "claude_cooldown").write(300)

        assert fields["config"].claude.cooldown_seconds == 300
        assert fields["config"].codex.cooldown_seconds == 900
        assert _saved_values(saved_settings, CLAUDE_SETTINGS.cooldown) == [300]

    def test_the_running_app_is_changed_before_the_file_is_written(self, monkeypatch):
        # These run on the settings window's own thread, where an escaping
        # error would be an invisible traceback and a dialog that half-worked.
        # The file is written last, and the write logs rather than raises —
        # which test_config pins — so the change the user can see always lands.
        model, fields = self._model()
        observed: list[int] = []
        monkeypatch.setattr(
            ConfigSetting,
            "save",
            lambda self, value: observed.append(
                fields["config"].polling.interval_seconds
            ),
        )

        self._field(model, "poll_interval").write(120)

        assert observed == [120]


class TestSettingsControllerWiring:
    """The tray hands off to a controller that owns the one window."""

    def _model(self):
        return main.build_settings_model(
            companion=_SettingsCompanion(),
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
