"""Nudge a provider's CLI when the usage API says its session or token is idle.

The tray has no way to mint a fresh OAuth token or to open a usage window — only
the provider's own CLI can. Asking it for one cheap reply makes it do both as a
side effect: it refreshes an expired token before sending, and the reply itself
starts the usage window so a real reset countdown appears.

Both providers use the same ``SessionNudger`` and the same rule for when to
run it. What differs — the argv, and how to read what the CLI prints — each
``Provider`` carries, so nothing in this module names a provider.

Each nudger reads its provider's settings on every poll: the two switches
(renew an expired token, wake an idle window), the cooldown, and the model and
effort to ask with. Each run is logged: the tokens it used, or why it failed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

from .models import CliReply, Provider, ProviderUsageData

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 120
# How long to wait for the output pipes to close once the CLI's tree is killed.
_KILLED_TREE_WAIT_SECONDS = 5
DEFAULT_COOLDOWN_SECONDS = 900  # 15 mins
MAX_CONSECUTIVE_FAILURES = 3
# How long a woken window is left alone: the length of one window.
WOKEN_WINDOW_SECONDS = 5 * 60 * 60

# What both CLIs are given in place of their own long system prompt. The
# default one, with its tool definitions, was most of the tokens a nudge cost.
NUDGE_INSTRUCTIONS = "Reply briefly."
# The effort a blank setting asks for. A one-word reply needs no reasoning.
DEFAULT_NUDGE_EFFORT = "low"

# A windowed build has no console, so an inherited one would flash on screen.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The folder both CLIs start in. It stays empty, so no AGENTS.md or CLAUDE.md
# is found in it or above it — under `uv run dev` the CLI used to start in this
# repository and read its AGENTS.md into every nudge.
_QUIET_FOLDER_NAME = "claudemonitor-cli"

# The two things one CLI run can fix.
NudgeReason = Literal["token_expired", "idle_window"]


@dataclass(frozen=True)
class RefreshOptions:
    """One provider's nudge settings, as they read at the moment of a poll."""

    renew_token: bool = True
    wake_session: bool = True
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    model: str = ""
    effort: str = ""

    def allows(self, reason: NudgeReason) -> bool:
        """Return whether the user lets the CLI run for this reason."""
        if reason == "token_expired":
            return self.renew_token
        return self.wake_session


def nudge_reason(data: ProviderUsageData) -> NudgeReason | None:
    """Return what a CLI run could fix in this fetch, or None if nothing.

    Two situations qualify: an expired token, which only the provider's own CLI
    can renew, and a completely untouched five-hour window — whether or not it
    has started — which one message converts into live usage the tray can count
    down. A fetch with no five-hour window at all is a shape we cannot read, and
    a message spent finding out what it meant would tell the user nothing.

    Both providers are judged by this one rule. Codex used to have a rule of its
    own that fired on an expired token alone, which left an idle Codex window —
    the state one message actually repairs — waiting for a manual `codex exec`.
    """
    if data.fetch_error is not None:
        return "token_expired" if data.fetch_error == "token_expired" else None
    if data.five_hour is None or data.five_hour.utilization > 0.0:
        return None
    return "idle_window"


def last_output_line(text: str) -> str:
    """Return the last line of output that is not blank."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def parsed_object(text: str) -> dict | None:
    """Parse text as one JSON object, or return None if it is not one."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def token_count(usage: dict, key: str) -> int | None:
    """Read one token count, ignoring anything that is not a whole number."""
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _quiet_folder() -> str | None:
    """Return an empty folder for the CLI to start in, or None to inherit ours."""
    folder = Path(tempfile.gettempdir()) / _QUIET_FOLDER_NAME
    try:
        folder.mkdir(exist_ok=True)
    except OSError as exc:
        log.warning("unable to create %s (%s); the CLI starts in the app's folder", folder, exc)
        return None
    return str(folder)


def _kill_process_tree(pid: int) -> None:
    """Stop a process and every process it started.

    `codex` is a .cmd shim, so the CLI runs as a grandchild of cmd.exe. Killing
    cmd.exe alone leaves the grandchild alive and holding the output pipes.
    """
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        creationflags=_CREATE_NO_WINDOW,
    )


def run_cli_process(
    args: list[str],
    *,
    timeout: float,
    capture_output: bool = False,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    kill_tree: Callable[[int], None] = _kill_process_tree,
    **popen_arguments,
) -> subprocess.CompletedProcess:
    """Run a command like subprocess.run, but kill its whole tree on a timeout.

    subprocess.run kills only the process it started, then waits for the
    output pipes to close. A grandchild that still holds them makes that wait
    last until the grandchild exits, which can be never.
    """
    if capture_output:
        popen_arguments.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = popen(args, **popen_arguments)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process.pid)
        try:
            process.communicate(timeout=_KILLED_TREE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning("the %s process tree outlived its kill", args[0])
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _run_cli(
    *,
    executable_name: str,
    arguments: tuple[str, ...],
    read_reply: Callable[[int, str, str], CliReply],
    which,
    run,
    clock: Callable[[], float] = time.monotonic,
) -> CliReply:
    """Run one provider CLI prompt, and report what it answered and how long it took.

    Never raises: this runs off the poll loop's thread, where an escaping error
    would be invisible in a windowed build.
    """
    executable = which(executable_name)
    if executable is None:
        return _failed(executable_name, f"The {executable_name} CLI was not found on PATH.")

    started_at = clock()
    reply = _run_and_read(executable_name, executable, arguments, read_reply, run)
    reply = replace(reply, duration_seconds=clock() - started_at)
    _log_reply(executable_name, reply)
    return reply


def _run_and_read(
    executable_name: str,
    executable: str,
    arguments: tuple[str, ...],
    read_reply: Callable[[int, str, str], CliReply],
    run,
) -> CliReply:
    """Start the CLI, wait for it, and read what it printed."""
    try:
        completed = run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            # Both CLIs write UTF-8. Windows' default code page cannot decode
            # every reply, and one character it cannot map would lose the rest.
            encoding="utf-8",
            errors="replace",
            # capture_output only redirects stdout and stderr, so stdin would
            # stay inherited — a console during `uv run dev`, an invalid handle
            # in the windowed build. The CLI folds piped stdin into its prompt,
            # so an inherited console lets keystrokes typed during a nudge become
            # part of the request. DEVNULL is an immediate EOF instead, which
            # also makes a CLI that wants to prompt fail fast rather than block.
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
            cwd=_quiet_folder(),
        )
    except subprocess.TimeoutExpired:
        return CliReply(
            succeeded=False,
            detail=f"The {executable_name} CLI did not answer within "
            f"{COMMAND_TIMEOUT_SECONDS} seconds.",
        )
    except OSError as exc:
        return CliReply(
            succeeded=False, detail=f"The {executable_name} CLI could not start: {exc}"
        )
    except Exception as exc:
        return CliReply(succeeded=False, detail=f"The {executable_name} CLI failed: {exc!r}")

    try:
        return read_reply(completed.returncode, completed.stdout or "", completed.stderr or "")
    except Exception as exc:
        return CliReply(
            succeeded=False,
            detail=f"The {executable_name} CLI reply could not be read: {exc!r}",
        )


def _failed(executable_name: str, detail: str) -> CliReply:
    """Log and return a run that failed before the CLI could start."""
    reply = CliReply(succeeded=False, detail=detail)
    _log_reply(executable_name, reply)
    return reply


def _log_reply(executable_name: str, reply: CliReply) -> None:
    """Write one line saying what a CLI run did."""
    if reply.succeeded:
        log.info(
            "%s CLI answered — token and session refreshed (%s tokens in, %s out)",
            executable_name,
            reply.input_tokens,
            reply.output_tokens,
        )
    else:
        log.warning("%s CLI refresh failed: %s", executable_name, reply.detail)


def run_provider_cli(
    provider: Provider,
    model: str = "",
    effort: str = "",
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CliReply:
    """Ask one provider's CLI for a throwaway reply, and say what it answered.

    Which executable, which arguments, and how to read the answer are the
    provider's own, so there is no per-provider wrapper here: a third provider
    adds nothing to this file. The lookup and the runner are resolved at call
    time so a test can stand in for either.
    """
    return _run_cli(
        executable_name=provider.cli_executable,
        arguments=provider.cli_arguments(model, effort),
        read_reply=provider.read_cli_reply,
        which=which or shutil.which,
        run=run or run_cli_process,
        clock=clock,
    )


def command_line(provider: Provider, model: str, effort: str) -> str:
    """Write the nudge as one line a user can paste into a terminal.

    It names the executable rather than the full path the app resolves, so the
    line reads the way the user would type it. Every argument is written so
    that cmd, PowerShell, and bash all pass it on unchanged.
    """
    return subprocess.list2cmdline([provider.cli_executable, *provider.cli_arguments(model, effort)])


def _start_daemon_thread(work: Callable[[], None]) -> None:
    """Run the CLI off the poll loop so the countdown keeps ticking meanwhile.

    A separate thread also means a CLI that fails or hangs cannot stop the
    poll loop.
    """
    threading.Thread(target=work, name="cli-session-nudge", daemon=True).start()


class SessionNudger:
    """Runs a provider CLI at most once per cooldown while its session looks idle.

    The API reflects a new session only after a short delay, so an ungated nudge
    would fire on every poll until the numbers caught up.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        options: Callable[[], RefreshOptions] = RefreshOptions,
        invoke: Callable[[RefreshOptions], CliReply] | None = None,
        on_refreshed: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        start_background: Callable[[Callable[[], None]], None] = _start_daemon_thread,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        cli_lock: threading.Lock | None = None,
    ) -> None:
        self.provider = provider
        # Held while this provider's CLI runs; shared with its ManualRun.
        self.cli_lock = cli_lock or threading.Lock()
        self._options = options
        self._invoke = invoke or (
            lambda chosen: run_provider_cli(provider, chosen.model, chosen.effort)
        )
        self._on_refreshed = on_refreshed
        self._clock = clock
        self._start_background = start_background
        self._max_consecutive_failures = max_consecutive_failures
        self._last_attempt_at: float | None = None
        self._consecutive_failures = 0
        # Set when a token renewal succeeded and no fetch has judged it yet.
        self._awaiting_confirmation = False
        # When the last successful wake finished; its window is left alone.
        self._woken_at: float | None = None

    @property
    def exhausted(self) -> bool:
        """Return whether repeated failures have given up on refreshing.

        The tray reads this to stop advising a refresh that cannot work and ask
        for a sign-in instead.
        """
        return self._consecutive_failures >= self._max_consecutive_failures

    # ENTRY POINT
    def maybe_nudge(self, data: ProviderUsageData) -> bool:
        """Start a background CLI refresh if this fetch warrants one, else do nothing.

        NUDGING RULES:
        1. Nudge only for a reason the user has switched on: an expired token,
           or a 5-hour window that has not started.
        2. Nudge at most once per the provider's own cooldown, in a separate
           non-blocking thread.
        3. Give up after 3 consecutive failures — dead credentials report
           `token_expired` forever and no prompt can fix them, so retrying is
           just a doomed subprocess every cooldown until the app restarts.
        4. A token renewal counts as a success only if the next fetch no
           longer shows an expired token. A 403 that is not about the token
           stays a 403; without this, it would run every cooldown.
        5. After a successful wake, leave the window alone for five hours. A
           lean nudge uses less than 1%, so the API still shows 0% after it.
           Counting that 0% as a failure tripped the breaker, and an idle
           window was then never woken again.
        6. Never run while this provider's CLI is already running, whether
           from here or from Run now.
        """
        reason = nudge_reason(data)
        if data.fetch_error is None and reason is None:
            # Whatever was broken has resolved, so past failures are stale.
            # Re-arming on any successful fetch would be wrong: a missing CLI
            # fails while fetches keep succeeding, and that would loop forever.
            self._consecutive_failures = 0
        self._judge_last_run(data, reason)

        if reason is None:
            return False
        if reason == "idle_window" and self._window_recently_woken():
            return False
        options = self._options()
        if not options.allows(reason):
            return False
        if self.exhausted or self._within_cooldown(options.cooldown_seconds):
            return False
        if not self.cli_lock.acquire(blocking=False):
            return False

        self._last_attempt_at = self._clock()
        self._start_background(lambda: self._nudge(options, reason))
        return True

    def _window_recently_woken(self) -> bool:
        """Return whether a successful wake started the window now in progress."""
        if self._woken_at is None:
            return False
        return self._clock() - self._woken_at < WOKEN_WINDOW_SECONDS

    def _judge_last_run(self, data: ProviderUsageData, reason: NudgeReason | None) -> None:
        """Count a successful run as a failure if this fetch still needs one.

        A fetch that failed for another reason says nothing either way, so the
        judgement waits for the next one.
        """
        if not self._awaiting_confirmation:
            return
        if reason is not None:
            self._awaiting_confirmation = False
            log.warning(
                "the %s CLI answered, but the next fetch still shows %s",
                self.provider.cli_executable,
                reason,
            )
            self._record_failure()
        elif data.fetch_error is None:
            self._awaiting_confirmation = False

    def _within_cooldown(self, cooldown_seconds: float) -> bool:
        """Return whether the previous attempt is still too recent to repeat."""
        if self._last_attempt_at is None:
            return False
        return self._clock() - self._last_attempt_at < cooldown_seconds

    def _record_success(self, reason: NudgeReason) -> None:
        """Note a run the CLI answered.

        A wake has started the window, and the clock for leaving it alone starts
        now, when the run ends: the window started while the run was going.
        A renewal is judged by the next fetch, which clears the failure count.
        """
        if reason == "idle_window":
            self._woken_at = self._clock()
            self._consecutive_failures = 0
        else:
            self._awaiting_confirmation = True

    def _record_failure(self) -> None:
        """Count one failed attempt and say so when it trips the breaker."""
        self._consecutive_failures += 1
        if self.exhausted:
            log.warning(
                "session refresh failed %s times — giving up until usage "
                "recovers; the provider CLI needs a manual sign-in",
                self._consecutive_failures,
            )

    def _nudge(self, options: RefreshOptions, reason: NudgeReason) -> None:
        """Run one CLI refresh and announce it, always letting go of the CLI after."""
        try:
            if self._invoke(options).succeeded:
                self._record_success(reason)
                self._on_refreshed()
            else:
                self._record_failure()
        except Exception:
            log.exception("session refresh failed")
            self._record_failure()
        finally:
            self.cli_lock.release()


# What the Run now button has done most recently.
ManualRunPhase = Literal["idle", "copied", "running", "finished"]


@dataclass(frozen=True)
class ManualRunState:
    """What the last manual action on a provider's command was, for display.

    ``command`` is the line that was copied or run. ``reply`` is set only once
    a run has finished.
    """

    phase: ManualRunPhase
    command: str = ""
    reply: CliReply | None = None


class ManualRun:
    """Runs a provider's nudge on request, one run at a time, and remembers the last one.

    This is the settings window's Run now button. It ignores the cooldown and
    the switches, because the user asked for it, but a success still asks the
    poll loop for fresh numbers, as an automatic nudge does. It waits for an
    automatic nudge of the same CLI to finish before it starts.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        invoke: Callable[[str, str], CliReply] | None = None,
        on_refreshed: Callable[[], None] = lambda: None,
        start_background: Callable[[Callable[[], None]], None] = _start_daemon_thread,
        cli_lock: threading.Lock | None = None,
    ) -> None:
        self.provider = provider
        # Held while this provider's CLI runs; shared with its SessionNudger.
        self.cli_lock = cli_lock or threading.Lock()
        self._invoke = invoke or (
            lambda model, effort: run_provider_cli(provider, model, effort)
        )
        self._on_refreshed = on_refreshed
        self._start_background = start_background
        self._lock = threading.Lock()
        self._state = ManualRunState(phase="idle")

    @property
    def state(self) -> ManualRunState:
        """Return what the last copy or run did."""
        return self._state

    def note_copied(self, command: str) -> None:
        """Remember that the command was copied, unless a run is still going."""
        with self._lock:
            if self._state.phase != "running":
                self._state = ManualRunState(phase="copied", command=command)

    def start(self, model: str, effort: str, finished: Callable[[], None]) -> bool:
        """Start one run in the background; False if one is already running.

        ``finished`` is called on the background thread once the run is over.
        """
        model = model.strip()
        with self._lock:
            if self._state.phase == "running":
                return False
            command = command_line(self.provider, model, effort)
            self._state = ManualRunState(phase="running", command=command)
        self._start_background(lambda: self._run(model, effort, command, finished))
        return True

    def _run(
        self, model: str, effort: str, command: str, finished: Callable[[], None]
    ) -> None:
        """Run the CLI, keep its reply, and tell the caller; never raise."""
        try:
            with self.cli_lock:
                reply = self._invoke(model, effort)
        except Exception as exc:
            log.exception("manual %s run failed", self.provider.cli_executable)
            reply = CliReply(succeeded=False, detail=f"The run failed: {exc!r}")
        with self._lock:
            self._state = ManualRunState(phase="finished", command=command, reply=reply)
        if reply.succeeded:
            self._on_refreshed()
        finished()
