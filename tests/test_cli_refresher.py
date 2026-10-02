from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from claudemonitor import cli_refresher
from claudemonitor.cli_refresher import (
    ManualRun,
    ManualRunState,
    RefreshOptions,
    SessionNudger,
    command_line,
    nudge_reason,
    run_provider_cli,
)
from claudemonitor.models import (
    CLAUDE,
    CODEX,
    PROVIDERS,
    CliReply,
    ProviderUsageData,
    UsageWindow,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

CLAUDE_REPLY = (
    '{"type":"result","is_error":false,"result":"Hi!",'
    '"usage":{"input_tokens":3,"cache_creation_input_tokens":100,'
    '"cache_read_input_tokens":900,"output_tokens":5}}'
)


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

    def __init__(self, returncode: int = 0, stdout: str = CLAUDE_REPLY, stderr: str = ""):
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


def _answers(succeeded: bool = True, **fields):
    """Build an invoker that records the options it was run with."""
    seen: list[RefreshOptions] = []

    def invoke(options: RefreshOptions) -> CliReply:
        seen.append(options)
        return CliReply(succeeded=succeeded, **fields)

    return invoke, seen


def _nudger(invoke, options: RefreshOptions | None = None, **kwargs) -> SessionNudger:
    """Build a synchronous nudger with a fixed clock unless the test overrides it."""
    settings = {
        "options": lambda: options or RefreshOptions(cooldown_seconds=0),
        "invoke": invoke,
        "start_background": _run_immediately,
        "clock": lambda: 0.0,
    }
    settings.update(kwargs)
    return SessionNudger(CLAUDE, **settings)


class TestSessionNudgeEndToEnd:
    """Fetched usage data in, Claude CLI invocation and recorded result out."""

    def _nudger(self, *, runner: _RecordingRunner, options=None):
        refreshed: list[str] = []
        nudger = SessionNudger(
            CLAUDE,
            options=lambda: options or RefreshOptions(model="haiku"),
            invoke=lambda chosen: run_provider_cli(
                CLAUDE,
                model=chosen.model,
                effort=chosen.effort,
                which=_which_finds_claude,
                run=runner,
            ),
            on_refreshed=lambda: refreshed.append("refreshed"),
            start_background=_run_immediately,
            clock=lambda: 0.0,
        )
        return nudger, refreshed

    def test_unstarted_session_runs_the_chosen_model_and_requests_a_refetch(self):
        runner = _RecordingRunner()
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(utilization=0.0, resets_at=None)) is True
        command = runner.calls[0][0]
        assert command[command.index("--model") + 1] == "haiku"
        assert command[-1] == "hi"
        assert refreshed == ["refreshed"]

    def test_the_chosen_effort_reaches_the_cli(self):
        runner = _RecordingRunner()
        nudger, _refreshed = self._nudger(
            runner=runner, options=RefreshOptions(model="haiku", effort="low")
        )

        nudger.maybe_nudge(usage(utilization=0.0))

        command = runner.calls[0][0]
        assert command[command.index("--effort") + 1] == "low"

    def test_a_successful_run_logs_its_tokens(self, caplog):
        nudger, _refreshed = self._nudger(runner=_RecordingRunner())

        with caplog.at_level(logging.INFO):
            nudger.maybe_nudge(usage(utilization=0.0))

        assert "1003 tokens in, 5 out" in caplog.text

    def test_a_rejected_model_logs_the_cli_s_own_reason(self, caplog):
        runner = _RecordingRunner(
            _CompletedProcess(
                returncode=1,
                stdout='{"type":"result","is_error":true,'
                '"result":"There\'s an issue with the selected model."}',
            )
        )
        nudger, refreshed = self._nudger(runner=runner)

        with caplog.at_level(logging.WARNING):
            nudger.maybe_nudge(usage(fetch_error="token_expired"))

        assert refreshed == []
        assert "There's an issue with the selected model." in caplog.text

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

    def test_failing_cli_does_not_propagate_or_claim_a_refresh(self):
        runner = _RecordingRunner(raises=OSError("cannot spawn"))
        nudger, refreshed = self._nudger(runner=runner)

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is True
        assert refreshed == []

    def test_repeated_expired_polls_only_nudge_once_per_cooldown(self):
        runner = _RecordingRunner()
        elapsed = [0.0]
        nudger = SessionNudger(
            CLAUDE,
            options=lambda: RefreshOptions(cooldown_seconds=900),
            invoke=lambda chosen: run_provider_cli(
                CLAUDE, which=_which_finds_claude, run=runner
            ),
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
        )

        expired = usage(fetch_error="token_expired")
        assert nudger.maybe_nudge(expired) is True
        elapsed[0] = 899.0
        assert nudger.maybe_nudge(expired) is False
        elapsed[0] = 900.0
        assert nudger.maybe_nudge(expired) is True
        assert len(runner.calls) == 2


class TestNudgeReason:
    """nudge_reason: which of the two things a CLI run could fix, if either."""

    def test_untouched_window_without_a_reset_time_is_an_idle_window(self):
        assert nudge_reason(usage(utilization=0.0, resets_at=None)) == "idle_window"

    def test_untouched_window_with_a_reset_time_is_an_idle_window(self):
        data = usage(utilization=0.0, resets_at=NOW + timedelta(hours=1))
        assert nudge_reason(data) == "idle_window"

    def test_expired_token_is_its_own_reason(self):
        assert nudge_reason(usage(fetch_error="token_expired")) == "token_expired"

    def test_any_usage_at_all_means_the_session_is_already_running(self):
        assert nudge_reason(usage(utilization=0.4)) is None

    def test_missing_credentials_cannot_be_fixed_by_a_prompt(self):
        assert nudge_reason(usage(fetch_error="no_credentials")) is None

    def test_transport_errors_say_nothing_about_the_session(self):
        for error in ("offline", "timeout", "rate_limited", "bad_response"):
            assert nudge_reason(usage(fetch_error=error)) is None

    def test_response_without_a_five_hour_window_is_not_evidence_of_an_idle_session(self):
        assert nudge_reason(usage()) is None

    def test_no_provider_specific_predicate_survives(self):
        # Two predicates meant two behaviours to keep in step, and they drifted.
        assert not hasattr(cli_refresher, "needs_codex_nudge")


class TestRefreshOptions:
    """Each reason has its own switch, so a user can allow one and not the other."""

    def test_both_reasons_are_allowed_by_default(self):
        options = RefreshOptions()
        assert options.allows("token_expired") is True
        assert options.allows("idle_window") is True

    def test_the_token_switch_only_governs_an_expired_token(self):
        options = RefreshOptions(renew_token=False)
        assert options.allows("token_expired") is False
        assert options.allows("idle_window") is True

    def test_the_session_switch_only_governs_an_idle_window(self):
        options = RefreshOptions(wake_session=False)
        assert options.allows("token_expired") is True
        assert options.allows("idle_window") is False


class TestTheSwitchesGateTheNudge:
    """The switches are read on every poll, so a change in Settings acts at once."""

    def test_an_expired_token_is_left_alone_when_renewing_is_off(self):
        invoke, seen = _answers()
        nudger = _nudger(invoke, RefreshOptions(renew_token=False, cooldown_seconds=0))

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is False
        assert seen == []

    def test_an_idle_window_is_still_woken_when_only_renewing_is_off(self):
        invoke, seen = _answers()
        nudger = _nudger(invoke, RefreshOptions(renew_token=False, cooldown_seconds=0))

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(seen) == 1

    def test_an_idle_window_is_left_alone_when_waking_is_off(self):
        invoke, seen = _answers()
        nudger = _nudger(invoke, RefreshOptions(wake_session=False, cooldown_seconds=0))

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False
        assert seen == []

    def test_both_switches_off_never_touch_the_cli(self):
        invoke, seen = _answers()
        nudger = _nudger(
            invoke,
            RefreshOptions(renew_token=False, wake_session=False, cooldown_seconds=0),
        )

        nudger.maybe_nudge(usage(utilization=0.0))
        nudger.maybe_nudge(usage(fetch_error="token_expired"))

        assert seen == []

    def test_options_are_read_again_on_every_poll(self):
        current = [RefreshOptions(wake_session=False, cooldown_seconds=0)]
        invoke, seen = _answers()
        nudger = SessionNudger(
            CLAUDE,
            options=lambda: current[0],
            invoke=invoke,
            start_background=_run_immediately,
            clock=lambda: 0.0,
        )
        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

        current[0] = RefreshOptions(wake_session=True, cooldown_seconds=0)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(seen) == 1

    def test_the_invoker_is_given_the_options_of_that_poll(self):
        invoke, seen = _answers()
        nudger = _nudger(
            invoke, RefreshOptions(model="gpt-5.5", effort="low", cooldown_seconds=0)
        )

        nudger.maybe_nudge(usage(utilization=0.0))

        assert (seen[0].model, seen[0].effort) == ("gpt-5.5", "low")


class TestTheCooldownIsLive:
    """Each provider's cooldown is its own and can change while the app runs."""

    def _nudger(self, cooldown: list[float], elapsed: list[float]):
        # A failed run, because a successful wake leaves the window alone.
        invoke, seen = _answers(succeeded=False)
        nudger = SessionNudger(
            CLAUDE,
            options=lambda: RefreshOptions(cooldown_seconds=cooldown[0]),
            invoke=invoke,
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
        )
        return nudger, seen

    def test_a_shorter_cooldown_ends_the_wait_early(self):
        cooldown, elapsed = [900.0], [0.0]
        nudger, _seen = self._nudger(cooldown, elapsed)
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        cooldown[0] = 60
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

    def test_a_longer_cooldown_takes_effect_at_once(self):
        cooldown, elapsed = [60.0], [0.0]
        nudger, _seen = self._nudger(cooldown, elapsed)
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        cooldown[0] = 900
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

    def test_switching_off_and_on_again_does_not_reset_the_cooldown(self):
        current = [RefreshOptions(cooldown_seconds=900)]
        elapsed = [0.0]
        invoke, _seen = _answers()
        nudger = SessionNudger(
            CLAUDE,
            options=lambda: current[0],
            invoke=invoke,
            start_background=_run_immediately,
            clock=lambda: elapsed[0],
        )
        assert nudger.maybe_nudge(usage(utilization=0.0)) is True

        current[0] = replace(current[0], wake_session=False)
        nudger.maybe_nudge(usage(utilization=0.0))
        current[0] = replace(current[0], wake_session=True)
        elapsed[0] = 100.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False


class TestRunProviderCli:
    """One runner for every provider: what the CLI answered, and never a raise."""

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

        run_provider_cli(
            provider,
            model="m",
            effort="low",
            which=lambda name: name + ".EXE",
            run=runner,
        )

        command, _kwargs = runner.calls[0]
        assert command == [
            provider.cli_executable + ".EXE",
            *provider.cli_arguments("m", "low"),
        ]

    def test_the_reply_is_read_by_the_provider(self):
        runner = _RecordingRunner(_CompletedProcess(stdout=CLAUDE_REPLY))

        reply = run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        assert reply.succeeded is True
        assert (reply.input_tokens, reply.output_tokens) == (1003, 5)
        assert reply.reply_text == "Hi!"

    def test_the_reply_says_how_long_the_cli_took(self):
        ticks = iter([10.0, 13.25])

        reply = run_provider_cli(
            CLAUDE, which=_which_finds_claude, run=_RecordingRunner(), clock=lambda: next(ticks)
        )

        assert reply.duration_seconds == 3.25

    def test_a_cli_that_never_started_has_no_duration(self):
        reply = run_provider_cli(CLAUDE, which=_which_finds_nothing, run=_RecordingRunner())

        assert reply.duration_seconds is None

    def test_a_failed_run_still_says_how_long_it_took(self):
        ticks = iter([0.0, 120.0])
        runner = _RecordingRunner(raises=subprocess.TimeoutExpired(cmd="claude", timeout=120))

        reply = run_provider_cli(
            CLAUDE, which=_which_finds_claude, run=runner, clock=lambda: next(ticks)
        )

        assert reply.duration_seconds == 120.0

    def test_the_cli_starts_in_an_empty_folder_of_its_own(self):
        # A CLI reads the instruction files of the folder it starts in. Under
        # `uv run dev` that was this repository, and its AGENTS.md.
        runner = _RecordingRunner()
        run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        folder = Path(kwargs["cwd"])
        assert folder.parent == Path(tempfile.gettempdir())
        assert folder.is_dir()
        assert list(folder.iterdir()) == []

    def test_a_missing_cli_says_so(self):
        runner = _RecordingRunner()

        reply = run_provider_cli(CLAUDE, which=_which_finds_nothing, run=runner)

        assert reply.succeeded is False
        assert "not found" in reply.detail
        assert runner.calls == []

    def test_a_cli_that_hangs_past_the_timeout_says_so(self):
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(cmd="claude", timeout=120)
        )

        reply = run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        assert reply.succeeded is False
        assert "120 seconds" in reply.detail

    def test_a_cli_that_cannot_be_launched_says_so(self):
        runner = _RecordingRunner(raises=OSError("not executable"))

        reply = run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        assert reply.succeeded is False
        assert "not executable" in reply.detail

    def test_never_lets_an_unexpected_error_escape(self):
        runner = _RecordingRunner(raises=RuntimeError("unexpected"))

        assert run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner).succeeded is False

    def test_a_reader_that_raises_is_a_failure_not_a_crash(self, monkeypatch):
        def broken(*_args):
            raise ValueError("bad reader")

        provider = replace(CLAUDE, read_cli_reply=broken)

        reply = run_provider_cli(provider, which=_which_finds_claude, run=_RecordingRunner())

        assert reply.succeeded is False

    def test_captures_output_under_a_timeout_and_hides_the_console_window(self):
        runner = _RecordingRunner()
        run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == cli_refresher.COMMAND_TIMEOUT_SECONDS
        assert kwargs["creationflags"] == cli_refresher._CREATE_NO_WINDOW

    def test_output_is_decoded_as_utf8(self):
        # Both CLIs write UTF-8; the Windows default code page cannot decode
        # every reply and would lose the whole result to one curly quote.
        runner = _RecordingRunner()
        run_provider_cli(CLAUDE, which=_which_finds_claude, run=runner)

        _command, kwargs = runner.calls[0]
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"

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

    def _nudger(self, *, succeeds: bool):
        invoke, seen = _answers(succeeded=succeeds)
        return _nudger(invoke), seen

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

    def test_a_confirmed_success_clears_the_failure_count(self):
        outcomes = iter([False, False, True, False, False])
        attempts: list[bool] = []

        def invoke(_options):
            attempts.append(True)
            return CliReply(succeeded=next(outcomes))

        nudger = _nudger(invoke)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)
        # The fetch after the success shows the token works again.
        nudger.maybe_nudge(usage(utilization=30.0, resets_at=NOW))
        nudger.maybe_nudge(expired)
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
        def explode(_options):
            raise RuntimeError("subprocess layer blew up")

        nudger = _nudger(explode)
        expired = usage(fetch_error="token_expired")

        for _ in range(3):
            nudger.maybe_nudge(expired)

        assert nudger.exhausted is True

    def test_the_failure_limit_is_configurable(self):
        invoke, attempts = _answers(succeeded=False)
        nudger = _nudger(invoke, max_consecutive_failures=1)
        expired = usage(fetch_error="token_expired")

        assert nudger.maybe_nudge(expired) is True
        assert nudger.maybe_nudge(expired) is False
        assert len(attempts) == 1

    def test_a_fresh_nudger_is_not_exhausted(self):
        nudger, _attempts = self._nudger(succeeds=False)
        assert nudger.exhausted is False


class TestConcurrentNudges:
    """A nudge outlives one poll, so a second poll must not stack another CLI call."""

    def test_a_nudge_still_running_blocks_a_second_one(self):
        pending: list = []
        invoke, started = _answers()
        nudger = _nudger(invoke, start_background=pending.append)

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

        pending[0]()  # the background thread finishes
        assert len(started) == 1

    def test_the_gate_reopens_after_the_background_work_finishes(self):
        elapsed = [0.0]
        pending: list = []
        invoke, _started = _answers()
        nudger = _nudger(
            invoke,
            RefreshOptions(cooldown_seconds=60),
            start_background=pending.append,
            clock=lambda: elapsed[0],
        )

        expired = usage(fetch_error="token_expired")
        nudger.maybe_nudge(expired)
        pending[0]()
        elapsed[0] = 61.0

        assert nudger.maybe_nudge(expired) is True


class TestTheCommandToCopy:
    """The one line a user can paste into a terminal to run the same nudge."""

    def test_it_is_the_executable_and_the_providers_own_arguments(self):
        assert command_line(CLAUDE, "haiku", "low") == subprocess.list2cmdline(
            ["claude", *CLAUDE.cli_arguments("haiku", "low")]
        )

    def test_it_names_the_executable_rather_than_its_full_path(self):
        assert command_line(CLAUDE, "", "").startswith("claude -p ")

    def test_an_argument_with_a_space_is_quoted(self):
        assert '--system-prompt "Reply briefly."' in command_line(CLAUDE, "", "")

    def test_the_codex_command_is_built_the_same_way(self, monkeypatch):
        monkeypatch.setattr(
            "claudemonitor.codex_fetcher._configured_mcp_servers", lambda: ()
        )

        line = command_line(CODEX, "", "")

        assert line.startswith("codex exec --json ")
        assert " -c model_reasoning_effort=low " in line
        assert line.endswith(" hi")


def _manual_run(invoke=None, **kwargs) -> ManualRun:
    """Build a manual run that finishes before start() returns."""
    settings = {
        "invoke": invoke or (lambda model, effort: CliReply(succeeded=True)),
        "start_background": _run_immediately,
    }
    settings.update(kwargs)
    return ManualRun(CLAUDE, **settings)


class TestAManualRun:
    """The Run now button: one run at a time, and what the last one did."""

    def test_nothing_has_run_at_first(self):
        assert _manual_run().state == ManualRunState(phase="idle")

    def test_a_run_asks_the_cli_with_the_model_and_effort_given(self):
        asked: list[tuple[str, str]] = []
        run = _manual_run(lambda model, effort: asked.append((model, effort)) or CliReply(True))

        run.start("haiku", "high", finished=lambda: None)

        assert asked == [("haiku", "high")]

    def test_a_model_is_trimmed_before_it_reaches_the_cli(self):
        asked: list[str] = []
        run = _manual_run(lambda model, effort: asked.append(model) or CliReply(True))

        run.start("  haiku ", "", finished=lambda: None)

        assert asked == ["haiku"]

    def test_a_finished_run_keeps_its_reply_and_its_command(self):
        reply = CliReply(succeeded=True, reply_text="Hi!", input_tokens=600)
        run = _manual_run(lambda model, effort: reply)

        run.start("haiku", "", finished=lambda: None)

        assert run.state == ManualRunState(
            phase="finished", command=command_line(CLAUDE, "haiku", ""), reply=reply
        )

    def test_the_caller_is_told_when_the_run_finishes(self):
        told: list[str] = []
        run = _manual_run()

        run.start("", "", finished=lambda: told.append("done"))

        assert told == ["done"]

    def test_a_run_in_progress_says_so(self):
        run = _manual_run(start_background=lambda work: None)

        assert run.start("haiku", "", finished=lambda: None) is True
        assert run.state == ManualRunState(
            phase="running", command=command_line(CLAUDE, "haiku", "")
        )

    def test_a_second_run_is_refused_while_one_is_in_progress(self):
        started: list[object] = []
        run = _manual_run(start_background=started.append)
        run.start("", "", finished=lambda: None)

        assert run.start("", "", finished=lambda: None) is False
        assert len(started) == 1

    def test_a_finished_run_can_be_run_again(self):
        run = _manual_run()
        run.start("", "", finished=lambda: None)

        assert run.start("", "", finished=lambda: None) is True

    def test_a_success_asks_for_fresh_usage(self):
        refreshed: list[str] = []
        run = _manual_run(on_refreshed=lambda: refreshed.append("poll"))

        run.start("", "", finished=lambda: None)

        assert refreshed == ["poll"]

    def test_a_failure_does_not_ask_for_fresh_usage(self):
        refreshed: list[str] = []
        run = _manual_run(
            lambda model, effort: CliReply(succeeded=False, detail="Not logged in"),
            on_refreshed=lambda: refreshed.append("poll"),
        )

        run.start("", "", finished=lambda: None)

        assert refreshed == []

    def test_an_invoker_that_raises_is_a_failed_run_not_a_crash(self, caplog):
        def broken(model, effort):
            raise RuntimeError("boom")

        told: list[str] = []
        run = _manual_run(broken)

        with caplog.at_level(logging.ERROR):
            run.start("", "", finished=lambda: told.append("done"))

        assert run.state.phase == "finished"
        assert run.state.reply.succeeded is False
        assert "boom" in run.state.reply.detail
        assert told == ["done"]

    def test_a_copied_command_is_remembered(self):
        run = _manual_run()

        run.note_copied("claude -p hi")

        assert run.state == ManualRunState(phase="copied", command="claude -p hi")

    def test_copying_during_a_run_does_not_hide_the_run(self):
        run = _manual_run(start_background=lambda work: None)
        run.start("", "", finished=lambda: None)

        run.note_copied("claude -p hi")

        assert run.state.phase == "running"

    def test_by_default_it_runs_its_own_providers_cli(self):
        assert ManualRun(CODEX).provider is CODEX


class TestAWokenWindowIsLeftAlone:
    """A lean nudge uses less than 1% of a window, so the API goes on showing
    0% after it. The window has started all the same. After a successful wake
    the window is left alone until it has had time to end, and the 0% is not
    held against the run. Counting it as a failure tripped the breaker after
    three wakes, and an idle window was then never woken again."""

    FIVE_HOURS = 5 * 60 * 60

    def _nudger(self, elapsed: list[float], succeeded: bool = True):
        invoke, attempts = _answers(succeeded=succeeded)
        nudger = _nudger(
            invoke, RefreshOptions(cooldown_seconds=60), clock=lambda: elapsed[0]
        )
        return nudger, attempts

    def test_a_window_still_at_zero_is_not_woken_again_inside_five_hours(self):
        elapsed = [0.0]
        nudger, attempts = self._nudger(elapsed)

        nudger.maybe_nudge(usage(utilization=0.0, resets_at=NOW))
        for minute in range(1, 300):
            elapsed[0] = minute * 60.0
            nudger.maybe_nudge(usage(utilization=0.0, resets_at=NOW))

        assert len(attempts) == 1

    def test_the_next_window_is_woken_once_five_hours_have_passed(self):
        elapsed = [0.0]
        nudger, attempts = self._nudger(elapsed)

        nudger.maybe_nudge(usage(utilization=0.0))
        elapsed[0] = self.FIVE_HOURS

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(attempts) == 2

    def test_the_five_hours_count_from_the_end_of_the_run(self):
        # The window starts when the request lands, which is during the run.
        # Counting from its start would wake the old window seconds before it
        # ends, and then leave the new one idle for five hours.
        elapsed = [0.0]

        def slow_invoke(_options):
            elapsed[0] += 30.0
            return CliReply(succeeded=True)

        nudger = _nudger(
            slow_invoke, RefreshOptions(cooldown_seconds=60), clock=lambda: elapsed[0]
        )

        nudger.maybe_nudge(usage(utilization=0.0))
        elapsed[0] = self.FIVE_HOURS + 29.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is False

    def test_wakes_that_stay_at_zero_never_trip_the_breaker(self):
        elapsed = [0.0]
        nudger, attempts = self._nudger(elapsed)

        for window in range(10):
            elapsed[0] = window * self.FIVE_HOURS
            nudger.maybe_nudge(usage(utilization=0.0, resets_at=NOW))
            elapsed[0] += 60.0
            nudger.maybe_nudge(usage(utilization=0.0, resets_at=NOW))

        assert len(attempts) == 10
        assert nudger.exhausted is False

    def test_a_failed_wake_is_tried_again_after_the_cooldown(self):
        elapsed = [0.0]
        nudger, attempts = self._nudger(elapsed, succeeded=False)

        nudger.maybe_nudge(usage(utilization=0.0))
        elapsed[0] = 60.0

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(attempts) == 2

    def test_a_successful_wake_clears_earlier_failures(self):
        outcomes = iter([False, False, True])
        elapsed = [0.0]

        def invoke(_options):
            return CliReply(succeeded=next(outcomes))

        nudger = _nudger(
            invoke, RefreshOptions(cooldown_seconds=60), clock=lambda: elapsed[0]
        )
        for attempt in range(3):
            elapsed[0] = attempt * 60.0
            nudger.maybe_nudge(usage(utilization=0.0))

        assert nudger._consecutive_failures == 0

    def test_an_expired_token_is_renewed_even_inside_the_five_hours(self):
        elapsed = [0.0]
        nudger, attempts = self._nudger(elapsed)

        nudger.maybe_nudge(usage(utilization=0.0))
        elapsed[0] = 60.0

        assert nudger.maybe_nudge(usage(fetch_error="token_expired")) is True
        assert len(attempts) == 2


class TestARenewalTheNextFetchDoesNotConfirm:
    """A CLI that answers has not always renewed the token: a 403 that is not
    about the token stays a 403. A renewal the next fetch does not confirm
    counts as a failure, so the breaker stops the runs rather than spending
    usage every cooldown for ever."""

    def test_a_token_that_stays_expired_stops_after_three_runs(self):
        invoke, attempts = _answers(succeeded=True)
        nudger = _nudger(invoke)

        for _ in range(10):
            nudger.maybe_nudge(usage(fetch_error="token_expired"))

        assert len(attempts) == 3
        assert nudger.exhausted is True

    def test_a_run_the_next_fetch_confirms_is_a_success(self):
        invoke, attempts = _answers(succeeded=True)
        nudger = _nudger(invoke)

        for _ in range(5):
            nudger.maybe_nudge(usage(fetch_error="token_expired"))
            nudger.maybe_nudge(usage(utilization=4.0, resets_at=NOW))

        assert len(attempts) == 5
        assert nudger.exhausted is False

    def test_a_fetch_that_fails_for_another_reason_waits_for_the_next_one(self):
        invoke, _attempts = _answers(succeeded=True)
        nudger = _nudger(invoke, max_consecutive_failures=1)

        nudger.maybe_nudge(usage(fetch_error="token_expired"))
        nudger.maybe_nudge(usage(fetch_error="offline"))
        nudger.maybe_nudge(usage(utilization=4.0, resets_at=NOW))

        assert nudger.exhausted is False

    def test_one_unconfirmed_run_is_counted_once(self):
        invoke, attempts = _answers(succeeded=True)
        elapsed = [0.0]
        nudger = _nudger(
            invoke, RefreshOptions(cooldown_seconds=60), clock=lambda: elapsed[0]
        )

        nudger.maybe_nudge(usage(fetch_error="token_expired"))
        # Polls inside the cooldown see the same expired token again and again.
        for _ in range(5):
            nudger.maybe_nudge(usage(fetch_error="token_expired"))

        assert len(attempts) == 1
        assert nudger.exhausted is False


class TestOneCliRunAtATimePerProvider:
    """Two runs of the same CLI can both refresh one token. OpenAI rotates the
    refresh token, so the run that loses can sign the user out."""

    def test_a_nudge_waits_while_a_manual_run_holds_the_cli(self):
        lock = threading.Lock()
        invoke, nudges = _answers()
        nudger = _nudger(invoke, cli_lock=lock)
        seen_during_run: list[bool] = []

        def manual_invoke(_model, _effort):
            seen_during_run.append(nudger.maybe_nudge(usage(utilization=0.0)))
            return CliReply(succeeded=True)

        _manual_run(manual_invoke, cli_lock=lock).start("", "", finished=lambda: None)

        assert seen_during_run == [False]
        assert nudges == []

    def test_a_refused_nudge_does_not_start_the_cooldown(self):
        lock = threading.Lock()
        invoke, nudges = _answers()
        nudger = _nudger(invoke, RefreshOptions(cooldown_seconds=60), cli_lock=lock)

        with lock:
            assert nudger.maybe_nudge(usage(utilization=0.0)) is False

        assert nudger.maybe_nudge(usage(utilization=0.0)) is True
        assert len(nudges) == 1

    def test_a_manual_run_waits_for_a_nudge_to_finish(self):
        lock = threading.Lock()
        pending: list = []
        invoke, _nudges = _answers()
        nudger = _nudger(invoke, start_background=pending.append, cli_lock=lock)
        manual_started = threading.Event()

        def manual_invoke(_model, _effort):
            manual_started.set()
            return CliReply(succeeded=True)

        run = _manual_run(manual_invoke, cli_lock=lock, start_background=_start_thread)

        nudger.maybe_nudge(usage(fetch_error="token_expired"))
        assert run.start("", "", finished=lambda: None) is True
        assert manual_started.wait(0.2) is False
        assert run.state.phase == "running"

        pending[0]()  # the nudge finishes and lets go of the CLI

        assert manual_started.wait(5) is True

    def test_a_nudge_that_raises_still_lets_go_of_the_cli(self):
        lock = threading.Lock()

        def explode(_options):
            raise RuntimeError("subprocess layer blew up")

        _nudger(explode, cli_lock=lock).maybe_nudge(usage(utilization=0.0))

        assert lock.acquire(blocking=False) is True

    def test_a_manual_run_that_raises_still_lets_go_of_the_cli(self):
        lock = threading.Lock()

        def explode(_model, _effort):
            raise RuntimeError("subprocess layer blew up")

        _manual_run(explode, cli_lock=lock).start("", "", finished=lambda: None)

        assert lock.acquire(blocking=False) is True


def _start_thread(work):
    """Run background work on a real thread, as the application does."""
    threading.Thread(target=work, daemon=True).start()


class _FakeProcess:
    """A Popen stand-in whose first wait times out when asked to."""

    def __init__(self, *, hangs: bool):
        self.pid = 4242
        self.returncode = None if hangs else 0
        self._hangs = hangs
        self.waits: list[float | None] = []

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if self._hangs:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)
        return ("out", "err")


class TestRunningTheCliProcess:
    """`codex` is a .cmd shim, so the CLI is a grandchild of the process we
    start. Killing only the child leaves the grandchild holding the output
    pipes, and the wait for them never ends."""

    def test_a_process_that_finishes_gives_its_code_and_output(self):
        process = _FakeProcess(hangs=False)

        completed = cli_refresher.run_cli_process(
            ["codex"], timeout=5, popen=lambda *a, **k: process, kill_tree=_never_kill
        )

        assert (completed.returncode, completed.stdout, completed.stderr) == (0, "out", "err")

    def test_captured_output_is_piped(self):
        opened: list[dict] = []

        def popen(args, **kwargs):
            opened.append(kwargs)
            return _FakeProcess(hangs=False)

        cli_refresher.run_cli_process(
            ["codex"],
            timeout=5,
            capture_output=True,
            text=True,
            popen=popen,
            kill_tree=_never_kill,
        )

        assert opened[0]["stdout"] == subprocess.PIPE
        assert opened[0]["stderr"] == subprocess.PIPE
        assert opened[0]["text"] is True
        assert "capture_output" not in opened[0]

    def test_a_process_that_hangs_has_its_whole_tree_killed(self):
        process = _FakeProcess(hangs=True)
        killed: list[int] = []

        with pytest.raises(subprocess.TimeoutExpired):
            cli_refresher.run_cli_process(
                ["codex"], timeout=5, popen=lambda *a, **k: process, kill_tree=killed.append
            )

        assert killed == [4242]

    def test_the_wait_after_a_kill_is_itself_bounded(self):
        # A tree that survives the kill must not hang the caller either.
        process = _FakeProcess(hangs=True)

        with pytest.raises(subprocess.TimeoutExpired):
            cli_refresher.run_cli_process(
                ["codex"], timeout=5, popen=lambda *a, **k: process, kill_tree=lambda pid: None
            )

        assert len(process.waits) == 2
        assert process.waits[1] is not None

    def test_the_cli_is_run_this_way_unless_a_test_says_otherwise(self, monkeypatch):
        runner = _RecordingRunner()
        monkeypatch.setattr(cli_refresher, "run_cli_process", runner)

        run_provider_cli(CLAUDE, which=_which_finds_claude)

        assert len(runner.calls) == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="taskkill is Windows only")
    def test_a_real_grandchild_holding_the_pipes_does_not_block_the_timeout(self):
        grandchild = "import time; time.sleep(30)"
        child = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
        )
        started_at = time.monotonic()

        with pytest.raises(subprocess.TimeoutExpired):
            cli_refresher.run_cli_process(
                [sys.executable, "-c", child], timeout=2, capture_output=True, text=True
            )

        assert time.monotonic() - started_at < 15


def _never_kill(pid: int) -> None:
    raise AssertionError(f"process {pid} should not have been killed")
