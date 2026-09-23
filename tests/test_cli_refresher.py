from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from claudemonitor import cli_refresher
from claudemonitor.cli_refresher import (
    SessionNudger,
    needs_session_nudge,
    run_provider_cli,
)
from claudemonitor.models import CLAUDE, CODEX, PROVIDERS, ProviderUsageData, UsageWindow

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def usage(
    *,
    utilization: float | None = None,
    resets_at: datetime | None = None,
    fetch_error: str | None = None,
) -> ProviderUsageData:
    """Build one fetch result, with a 5h window only when a utilization is given."""
    five_hour = (
        UsageWindow(utilization=utilization, resets_at=resets_at)
        if utilization is not None
        else None
    )
    return ProviderUsageData(five_hour=five_hour, fetch_error=fetch_error, fetched_at=NOW)


class _CompletedProcess:
    """Stand-in for subprocess.CompletedProcess with only the fields we read."""

    def __init__(self, returncode: int = 0, stdout: str = "Hello!", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRunner:
    """Captures subprocess invocations and replays a scripted result."""

    def __init__(self, result=None, raises: Exception | None = None):
        self._result = result if result is not None else _CompletedProcess()
        self._raises = raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._result


def _which_finds_claude(_name: str) -> str:
    return r"C:\Users\someone\.local\bin\claude.EXE"


def _which_finds_codex(_name: str) -> str:
    return r"C:\Users\someone\AppData\Roaming\npm\codex.CMD"


def _which_finds_nothing(_name: str) -> None:
    return None


def _run_immediately(work):
    """Replace the background thread so tests observe the nudge synchronously."""
    work()


class TestSessionNudgeEndToEnd:
    """Fetched usage data in, Claude CLI invocation (or not) out."""

    def _nudger(self, *, runner: _RecordingRunner, **kwargs) -> tuple[SessionNudger, list[str]]:
        refreshed: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner),
            on_refreshed=lambda: refreshed.append("refreshed"),
            start_background=_run_immediately,
            clock=lambda: 0.0,
            **kwargs,
        )
        return nudger, refreshed

    def test_unstarted_session_runs_the_haiku_prompt_and_requests_a_refetch(self):
        runner = _RecordingRunner()
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(utilization=0.0, resets_at=None)) is True
        assert runner.calls[0][0][1:] == ["-p", "--model", "haiku", "hi"]
        assert refreshed == ["refreshed"]

    def test_full_window_with_an_active_reset_time_also_nudges(self):
        runner = _RecordingRunner()
        nudger, _refreshed = self._nudger(runner=runner)

        data = usage(utilization=0.0, resets_at=NOW + timedelta(hours=3))

        assert nudger.maybe_nudge(data) is True
        assert len(runner.calls) == 1

    def test_expired_token_nudges_so_claude_code_can_refresh_it(self):
        runner = _RecordingRunner()
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is True
        assert refreshed == ["refreshed"]

    def test_partly_used_window_is_left_alone(self):
        runner = _RecordingRunner()
        nudger, refreshed = self._nudger(runner=runner)

        data = usage(utilization=12.5, resets_at=NOW + timedelta(hours=3))

        assert nudger.maybe_nudge(data) is False
        assert runner.calls == []
        assert refreshed == []

    def test_silent_cli_output_does_not_claim_a_refresh_happened(self):
        runner = _RecordingRunner(_CompletedProcess(stdout="   \n"))
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(runner.calls) == 1
        assert refreshed == []

    def test_failing_cli_does_not_propagate_or_claim_a_refresh(self):
        runner = _RecordingRunner(raises=OSError("cannot spawn"))
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is True
        assert refreshed == []

    def test_disabled_nudger_never_touches_the_cli(self):
        runner = _RecordingRunner()
        nudger, _refreshed = self._nudger(runner=runner, enabled=False)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False
        assert runner.calls == []

    def test_repeated_unstarted_polls_only_nudge_once_per_cooldown(self):
        runner = _RecordingRunner()
        elapsed = [0.0]
        refreshed: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner),
            on_refreshed=lambda: refreshed.append("refreshed"),
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
            cooldown_seconds=900,
        )

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        elapsed[0] = 899.0
        assert nudger.maybe_nudge(usage(utilization=0.0)) is False
        elapsed[0] = 900.0
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(runner.calls) == 2


class TestNeedsSessionNudge:
    """needs_session_nudge: decides whether a fetch result warrants a CLI call."""

    def test_untouched_window_without_a_reset_time_needs_a_nudge(self):
        assert needs_session_nudge(usage(utilization=0.0, resets_at=None)) is True

    def test_untouched_window_with_a_reset_time_needs_a_nudge(self):
        data = usage(utilization=0.0, resets_at=NOW + timedelta(hours=1))
        assert needs_session_nudge(data) is True

    def test_expired_token_needs_a_nudge(self):
        assert needs_session_nudge(usage(fetch_error="token_expired")) is True

    def test_any_usage_at_all_means_the_session_is_already_running(self):
        assert needs_session_nudge(usage(utilization=0.4)) is False

    def test_missing_credentials_cannot_be_fixed_by_a_prompt(self):
        assert needs_session_nudge(usage(fetch_error="no_credentials")) is False

    def test_transport_errors_say_nothing_about_the_session(self):
        assert needs_session_nudge(usage(fetch_error="offline")) is False
        assert needs_session_nudge(usage(fetch_error="timeout")) is False
        assert needs_session_nudge(usage(fetch_error="rate_limited")) is False
        assert needs_session_nudge(usage(fetch_error="bad_response")) is False

    def test_response_without_a_five_hour_window_is_not_evidence_of_an_idle_session(self):
        assert needs_session_nudge(usage()) is False


class TestWhatEachProviderAsksItsCli:
    """The argv is the provider's own data now, not a function per provider."""

    def test_claude_asks_haiku_for_the_cheapest_possible_reply(self):
        assert CLAUDE.cli_arguments == ("-p", "--model", "haiku", "hi")

    def test_codex_runs_a_non_interactive_turn(self):
        assert CODEX.cli_arguments[0] == "exec"
        assert CODEX.cli_arguments[-1] == "hi"

    def test_codex_refuses_to_touch_the_users_files(self):
        # The nudge only exists to make one authenticated request. A sandbox
        # that could write would let an off-hand model reply edit real files.
        arguments = CODEX.cli_arguments
        assert arguments[arguments.index("--sandbox") + 1] == "read-only"

    def test_codex_does_not_require_a_git_repository(self):
        # The tray app runs from wherever Windows launched it, which is usually
        # not a repository; without this the CLI refuses to start at all.
        assert "--skip-git-repo-check" in CODEX.cli_arguments

    def test_every_provider_names_an_executable_and_a_prompt(self):
        for provider in PROVIDERS:
            assert provider.cli_executable, provider
            assert provider.cli_arguments, provider


class TestRunProviderCli:
    """One runner for every provider: "did the CLI answer?", and never a raise."""

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_providers_own_executable_is_looked_up(self, provider):
        looked_up: list[str] = []

        def which(name):
            looked_up.append(name)
            return name + ".EXE"

        run_provider_cli(provider, which=which, run=_RecordingRunner())

        assert looked_up == [provider.cli_executable]

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_resolved_executable_is_run_with_the_providers_arguments(
        self, provider
    ):
        runner = _RecordingRunner()

        run_provider_cli(provider, which=lambda name: name + ".EXE", run=runner)

        command, _kwargs = runner.calls[0]
        assert command == [provider.cli_executable + ".EXE", *provider.cli_arguments]

    def test_reports_success_when_the_cli_prints_a_reply(self):
        runner = _RecordingRunner(_CompletedProcess(stdout="Hello! How can I help?"))
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is True

    def test_reports_failure_when_the_cli_is_not_installed(self):
        runner = _RecordingRunner()
        assert run_provider_cli(CLAUDE, which=_which_finds_nothing, run=runner) is False
        assert runner.calls == []

    def test_reports_failure_on_a_non_zero_exit_code(self):
        runner = _RecordingRunner(
            _CompletedProcess(returncode=1, stdout="", stderr="boom")
        )
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is False

    def test_reports_failure_on_empty_output(self):
        runner = _RecordingRunner(_CompletedProcess(stdout="   "))
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is False

    def test_reports_failure_when_the_cli_hangs_past_the_timeout(self):
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(cmd="claude", timeout=120)
        )
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is False

    def test_reports_failure_when_the_cli_cannot_be_launched(self):
        runner = _RecordingRunner(raises=OSError("not executable"))
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is False

    def test_never_lets_an_unexpected_error_escape(self):
        runner = _RecordingRunner(raises=RuntimeError("unexpected"))
        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner) is False

    def test_captures_output_under_a_timeout_and_hides_the_console_window(self):
        runner = _RecordingRunner()
        run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == cli_refresher.COMMAND_TIMEOUT_SECONDS
        assert kwargs["creationflags"] == cli_refresher._CREATE_NO_WINDOW

    def test_stdin_is_closed_so_the_cli_cannot_read_the_terminal(self):
        """`capture_output` redirects only stdout/stderr, leaving stdin inherited.

        The CLI merges piped stdin into its prompt, so an inherited console would
        let whatever the user types during a nudge become part of the request.
        """
        runner = _RecordingRunner()
        run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_a_nudger_given_no_invoker_runs_its_own_providers_cli(self):
        # This is what deleted create_codex_nudger: a nudger knows which CLI
        # answers for it, so nothing has to pair the two up from outside.
        assert SessionNudger(CODEX).provider is CODEX


class TestCircuitBreaker:
    """Dead credentials keep reporting `token_expired` forever, and no prompt can
    fix that — so repeated failures must stop the retries instead of running one
    doomed subprocess every cooldown until the machine is rebooted."""

    def _nudger(self, *, succeeds: bool, cooldown_seconds: float = 0.0):
        attempts: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: attempts.append("attempt") or succeeds,
            start_background=_run_immediately,
            clock=lambda: 0.0,
            cooldown_seconds=cooldown_seconds,
        )
        return nudger, attempts

    def test_three_failures_stop_any_further_attempts(self):
        nudger, attempts = self._nudger(succeeds=False)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            assert nudger.maybe_nudge(expired) is True

        assert nudger.maybe_nudge(expired) is False
        assert nudger.maybe_nudge(expired) is False
        assert len(attempts) == 3

    def test_the_breaker_is_not_tripped_before_the_limit(self):
        nudger, _attempts = self._nudger(succeeds=False)
        expired = usage(fetch_error="token_expired")

        nudger.maybe_nudge(expired)
        nudger.maybe_nudge(expired)

        assert nudger.exhausted is False

    def test_exhausted_reports_the_tripped_breaker(self):
        nudger, _attempts = self._nudger(succeeds=False)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)

        assert nudger.exhausted is True

    def test_a_success_clears_the_failure_count(self):
        attempts: list[bool] = []
        outcomes = iter([False, False, True, False, False])
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: attempts.append(True) or next(outcomes),
            start_background=_run_immediately,
            clock=lambda: 0.0,
            cooldown_seconds=0.0,
        )
        expired = usage(fetch_error="token_expired")

        for _ in range(5):
            nudger.maybe_nudge(expired)

        # Two failures, a success that reset the count, then two more failures.
        assert nudger.exhausted is False
        assert len(attempts) == 5

    def test_a_healthy_fetch_rearms_the_breaker(self):
        nudger, attempts = self._nudger(succeeds=False)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)
        assert nudger.exhausted is True

        # The user signed in again and started using Claude.
        nudger.maybe_nudge(usage(utilization=30.0, resets_at=NOW))

        assert nudger.exhausted is False
        assert nudger.maybe_nudge(expired) is True
        assert len(attempts) == 4

    def test_a_still_broken_fetch_does_not_rearm_the_breaker(self):
        nudger, attempts = self._nudger(succeeds=False)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)

        # Nothing has been fixed, so neither of these may reopen the gate.
        nudger.maybe_nudge(usage(fetch_error="token_expired"))
        nudger.maybe_nudge(usage(utilization=0.0))

        assert nudger.exhausted is True
        assert len(attempts) == 3

    def test_an_exception_from_the_cli_counts_as_a_failure(self):
        def explode() -> bool:
            raise RuntimeError("subprocess layer blew up")

        nudger = SessionNudger(
            CLAUDE,
            invoke=explode,
            start_background=_run_immediately,
            clock=lambda: 0.0,
            cooldown_seconds=0.0,
        )
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)

        assert nudger.exhausted is True

    def test_the_failure_limit_is_configurable(self):
        attempts: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: attempts.append("attempt") or False,
            start_background=_run_immediately,
            clock=lambda: 0.0,
            cooldown_seconds=0.0,
            max_consecutive_failures=1,
        )
        expired = usage(fetch_error="token_expired")

        assert nudger.maybe_nudge(expired) is True
        assert nudger.maybe_nudge(expired) is False
        assert len(attempts) == 1

    def test_a_fresh_nudger_is_not_exhausted(self):
        nudger, _attempts = self._nudger(succeeds=False)
        assert nudger.exhausted is False


class TestRuntimeToggle:
    """The tray flips the nudger while it is running, so the switch is live state."""

    def _nudger(self, *, enabled: bool):
        started: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            enabled=enabled,
            invoke=lambda: started.append("invoked") or True,
            start_background=_run_immediately,
            clock=lambda: 0.0,
        )
        return nudger, started

    def test_enabled_reports_the_current_setting(self):
        nudger, _started = self._nudger(enabled=True)
        assert nudger.enabled is True

        nudger.set_enabled(False)

        assert nudger.enabled is False

    def test_enabling_at_runtime_allows_the_next_nudge(self):
        nudger, started = self._nudger(enabled=False)
        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

        nudger.set_enabled(True)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert started == ["invoked"]

    def test_disabling_at_runtime_stops_the_next_nudge(self):
        nudger, started = self._nudger(enabled=True)

        nudger.set_enabled(False)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False
        assert started == []

    def test_re_enabling_does_not_reset_the_cooldown(self):
        elapsed = [0.0]
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: True,
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
            cooldown_seconds=900,
        )
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        nudger.set_enabled(False)
        nudger.set_enabled(True)
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

    def test_the_cooldown_can_be_changed_while_running(self):
        # The settings window writes a new cooldown on Apply; a nudger that
        # kept the value it started with would ignore it until the next launch.
        elapsed = [0.0]
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: True,
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
            cooldown_seconds=900,
        )
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        nudger.set_cooldown_seconds(60)
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

    def test_a_longer_cooldown_takes_effect_at_once(self):
        elapsed = [0.0]
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: True,
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
            cooldown_seconds=60,
        )
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        nudger.set_cooldown_seconds(900)
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False


class TestConcurrentNudges:
    """A nudge outlives one poll, so a second poll must not stack another CLI call."""

    def test_a_nudge_still_running_blocks_a_second_one(self):
        started: list[ProviderUsageData] = []
        pending: list = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: started.append("invoked") or True,
            start_background=pending.append,
            clock=lambda: 0.0,
        )

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

        pending[0]()  # the background thread finishes
        assert len(started) == 1

    def test_the_gate_reopens_after_the_background_work_finishes(self):
        elapsed = [0.0]
        pending: list = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: True,
            start_background=pending.append,
            clock=lambda: elapsed[0],
            cooldown_seconds=60,
        )

        nudger.maybe_nudge(usage(utilization=0.0))
        pending[0]()
        elapsed[0] = 61.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True


# ===========================================================================
# Codex — the same nudge, decided by the same rule.
# ===========================================================================


class TestCodexSharesTheNudgeRule:
    """One rule decides for both providers, because one message fixes both.

    Codex used to own a predicate that fired on an expired token and nothing
    else, on the theory that Codex reports a reset time whether or not the
    window has been used. The effect was that an idle Codex window — the one
    state a single message actually repairs — was never woken, and the user
    had to run ``codex exec`` by hand.
    """

    def test_expired_token_is_worth_nudging(self):
        assert needs_session_nudge(usage(fetch_error="token_expired")) is True

    def test_untouched_window_is_worth_nudging(self):
        assert needs_session_nudge(usage(utilization=0.0)) is True

    def test_healthy_usage_is_left_alone(self):
        assert needs_session_nudge(usage(utilization=42.0)) is False

    def test_other_errors_are_not_nudgeable(self):
        for error in ("offline", "timeout", "no_credentials", "rate_limited"):
            assert needs_session_nudge(usage(fetch_error=error)) is False

    def test_no_provider_specific_predicate_survives(self):
        # Two predicates meant two behaviours to keep in step, and they drifted.
        assert not hasattr(cli_refresher, "needs_codex_nudge")


def _only_on_expiry(data: ProviderUsageData) -> bool:
    """A stub predicate that ignores everything except an expired token."""
    return data.fetch_error == "token_expired"


class TestNudgerHonorsItsPredicate:
    """SessionNudger asks the injected predicate, not the shared rule."""

    def test_an_injected_predicate_can_suppress_the_idle_window_nudge(self):
        calls: list[int] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: (calls.append(1), True)[1],
            needs_nudge=_only_on_expiry,
            start_background=_run_immediately,
        )

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False
        assert calls == []

    def test_an_injected_predicate_fires_on_what_it_accepts(self):
        calls: list[int] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: (calls.append(1), True)[1],
            needs_nudge=_only_on_expiry,
            start_background=_run_immediately,
        )

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is True
        assert calls == [1]

    def test_breaker_rearms_using_the_injected_predicate(self):
        # An idle window is "nothing left to fix" under this stub rule, so a
        # run of failures must be forgiven once fetches recover.
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: False,
            needs_nudge=_only_on_expiry,
            cooldown_seconds=0,
            start_background=_run_immediately,
        )
        for _ in range(3):
            nudger.maybe_nudge(usage(fetch_error="token_expired"))
        assert nudger.exhausted is True

        nudger.maybe_nudge(usage(utilization=0.0))

        assert nudger.exhausted is False

    def test_the_shared_rule_is_the_default_predicate(self):
        calls: list[int] = []
        nudger = SessionNudger(
            CLAUDE,
            invoke=lambda: (calls.append(1), True)[1],
            start_background=_run_immediately,
        )

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert calls == [1]
