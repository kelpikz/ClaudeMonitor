from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from claudemonitor import cli_refresher
from claudemonitor.cli_refresher import (
    SessionNudger,
    _claude_command,
    needs_session_nudge,
    run_claude_cli,
)
from claudemonitor.models import AnthropicUsageData, UsageWindow

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def usage(
    *,
    utilization: float | None = None,
    resets_at: datetime | None = None,
    fetch_error: str | None = None,
) -> AnthropicUsageData:
    """Build one fetch result, with a 5h window only when a utilization is given."""
    five_hour = (
        UsageWindow(utilization=utilization, resets_at=resets_at)
        if utilization is not None
        else None
    )
    return AnthropicUsageData(five_hour=five_hour, fetch_error=fetch_error, fetched_at=NOW)


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
            invoke=lambda: run_claude_cli(which=_which_finds_claude, run=runner),
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
            invoke=lambda: run_claude_cli(which=_which_finds_claude, run=runner),
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


class TestClaudeCommand:
    """_claude_command: the exact argv handed to the resolved Claude executable."""

    def test_asks_haiku_for_the_cheapest_possible_reply(self):
        assert _claude_command("claude.EXE") == [
            "claude.EXE",
            "-p",
            "--model",
            "haiku",
            "hi",
        ]


class TestRunClaudeCli:
    """run_claude_cli: turns one CLI invocation into 'did it answer?'."""

    def test_reports_success_when_the_cli_prints_a_reply(self):
        runner = _RecordingRunner(_CompletedProcess(stdout="Hello! How can I help?"))
        assert run_claude_cli(which=_which_finds_claude, run=runner) is True

    def test_reports_failure_when_claude_is_not_installed(self):
        runner = _RecordingRunner()
        assert run_claude_cli(which=_which_finds_nothing, run=runner) is False
        assert runner.calls == []

    def test_reports_failure_on_a_non_zero_exit_code(self):
        runner = _RecordingRunner(_CompletedProcess(returncode=1, stdout="", stderr="boom"))
        assert run_claude_cli(which=_which_finds_claude, run=runner) is False

    def test_reports_failure_on_empty_output(self):
        runner = _RecordingRunner(_CompletedProcess(stdout=""))
        assert run_claude_cli(which=_which_finds_claude, run=runner) is False

    def test_reports_failure_when_the_cli_hangs_past_the_timeout(self):
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(cmd="claude", timeout=120)
        )
        assert run_claude_cli(which=_which_finds_claude, run=runner) is False

    def test_never_lets_an_unexpected_error_escape(self):
        runner = _RecordingRunner(raises=RuntimeError("unexpected"))
        assert run_claude_cli(which=_which_finds_claude, run=runner) is False

    def test_captures_output_under_a_timeout_and_hides_the_console_window(self):
        runner = _RecordingRunner()
        run_claude_cli(which=_which_finds_claude, run=runner)

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
        run_claude_cli(which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        assert kwargs["stdin"] == subprocess.DEVNULL


class TestCircuitBreaker:
    """Dead credentials keep reporting `token_expired` forever, and no prompt can
    fix that — so repeated failures must stop the retries instead of running one
    doomed subprocess every cooldown until the machine is rebooted."""

    def _nudger(self, *, succeeds: bool, cooldown_seconds: float = 0.0):
        attempts: list[str] = []
        nudger = SessionNudger(
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


class TestConcurrentNudges:
    """A nudge outlives one poll, so a second poll must not stack another CLI call."""

    def test_a_nudge_still_running_blocks_a_second_one(self):
        started: list[AnthropicUsageData] = []
        pending: list = []
        nudger = SessionNudger(
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
            invoke=lambda: True,
            start_background=pending.append,
            clock=lambda: elapsed[0],
            cooldown_seconds=60,
        )

        nudger.maybe_nudge(usage(utilization=0.0))
        pending[0]()
        elapsed[0] = 61.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
