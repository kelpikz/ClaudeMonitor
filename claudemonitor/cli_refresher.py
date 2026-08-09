"""Nudge the Claude CLI when the usage API says the session or token is idle.

The tray has no way to mint a fresh OAuth token or to open a usage window — only
Claude Code can. Asking the CLI for one cheap Haiku reply makes it do both as a
side effect: it refreshes an expired token before sending, and the reply itself
starts the five-hour window so a real reset countdown appears.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable

from .models import AnthropicUsageData

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_COOLDOWN_SECONDS = 900 # 15 mins
MAX_CONSECUTIVE_FAILURES = 3

_EXECUTABLE_NAME = "claude"
_PROMPT_ARGUMENTS = ("-p", "--model", "haiku", "hi")
# A windowed build has no console, so an inherited one would flash on screen.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_NUDGEABLE_FETCH_ERRORS = frozenset({"token_expired"})


def needs_session_nudge(data: AnthropicUsageData) -> bool:
    """Return whether this fetch result describes a session the CLI could wake.

    Two situations qualify: an expired token, which only Claude Code can renew,
    and a completely untouched five-hour window — whether or not it has started —
    which one message converts into live usage the tray can count down.
    """
    if data.fetch_error is not None:
        return data.fetch_error in _NUDGEABLE_FETCH_ERRORS
    if data.five_hour is None:
        return False
    return data.five_hour.utilization <= 0.0


def _claude_command(executable: str) -> list[str]:
    """Build the argv for the cheapest prompt that still forces a real request."""
    return [executable, *_PROMPT_ARGUMENTS]


def run_claude_cli(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Ask the Claude CLI for one Haiku reply and report whether it answered.

    Never raises: this runs off the poll loop's thread, where an escaping error
    would be invisible in a windowed build.
    """
    executable = which(_EXECUTABLE_NAME)
    if executable is None:
        log.warning("claude CLI not found on PATH — skipping session refresh")
        return False

    try:
        completed = run(
            _claude_command(executable),
            capture_output=True,
            text=True,
            # capture_output only redirects stdout and stderr, so stdin would
            # stay inherited — a console during `uv run dev`, an invalid handle
            # in the windowed build. The CLI folds piped stdin into its prompt,
            # so an inherited console lets keystrokes typed during a nudge become
            # part of the request. DEVNULL is an immediate EOF instead, which
            # also makes a CLI that wants to prompt fail fast rather than block.
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        log.warning("claude CLI did not answer within %ss", COMMAND_TIMEOUT_SECONDS)
        return False
    except OSError as exc:
        log.warning("unable to launch claude CLI: %s", exc)
        return False
    except Exception as exc:
        log.warning("unexpected error running claude CLI: %r", exc)
        return False

    if completed.returncode != 0:
        log.warning(
            "claude CLI exited with %s: %s",
            completed.returncode,
            (completed.stderr or "").strip(),
        )
        return False

    if not (completed.stdout or "").strip():
        log.warning("claude CLI returned no output — session may not have started")
        return False

    log.info("claude CLI answered — token and session refreshed")
    return True


def _start_daemon_thread(work: Callable[[], None]) -> None:
    """Run the CLI off the poll loop so the countdown keeps ticking meanwhile.
    
    We are running in a separate thread because, 
    even if it fails, nothing will happen to our main code
    """
    threading.Thread(target=work, name="claude-session-nudge", daemon=True).start()


class SessionNudger:
    """Runs the Claude CLI at most once per cooldown while the session looks idle.

    The API reflects a new session only after a short delay, so an ungated nudge
    would fire on every poll until the numbers caught up.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        invoke: Callable[[], bool] = run_claude_cli,
        on_refreshed: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        start_background: Callable[[Callable[[], None]], None] = _start_daemon_thread,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._enabled = enabled
        self._cooldown_seconds = cooldown_seconds
        self._invoke = invoke
        self._on_refreshed = on_refreshed
        self._clock = clock
        self._start_background = start_background
        self._max_consecutive_failures = max_consecutive_failures
        self._last_attempt_at: float | None = None
        self._running = False
        self._consecutive_failures = 0

    @property
    def enabled(self) -> bool:
        """Return whether nudging is currently switched on."""
        return self._enabled

    @property
    def exhausted(self) -> bool:
        """Return whether repeated failures have given up on refreshing.

        The tray reads this to stop advising a refresh that cannot work and ask
        for a sign-in instead.
        """
        return self._consecutive_failures >= self._max_consecutive_failures

    def set_enabled(self, enabled: bool) -> None:
        """Switch nudging on or off while the poll loop is running.

        The cooldown is deliberately left untouched, so flipping the tray toggle
        cannot be used to bypass it.
        """
        self._enabled = enabled

    # ENTRY POINT
    def maybe_nudge(self, data: AnthropicUsageData) -> bool:
        """Start a background CLI refresh if this fetch warrants one, else do nothing.

        NUDGING RULES:
        1. Nudge only when the setting is enabled
        2. Nudge when session expired / 5 hour limit has not started.
        3. Nudge every 15 mins - in a separate not blocking thread
        4. Give up after 3 consecutive failures — dead credentials report
           `token_expired` forever and no prompt can fix them, so retrying is
           just a doomed subprocess every cooldown until the app restarts.
        """
        if self._nothing_left_to_fix(data):
            # Whatever was broken has resolved, so past failures are stale.
            self._consecutive_failures = 0

        if not self._enabled or self._running or not needs_session_nudge(data):
            return False
        if self.exhausted or self._within_cooldown():
            return False

        self._last_attempt_at = self._clock()
        self._running = True
        self._start_background(self._nudge)
        return True

    def _nothing_left_to_fix(self, data: AnthropicUsageData) -> bool:
        """Return whether a healthy fetch shows there is nothing to nudge about.

        This is what re-arms the breaker. Re-arming on any successful fetch would
        be wrong: a missing CLI fails while fetches keep succeeding, and that
        would loop forever. Requiring live usage means the underlying problem is
        genuinely gone.
        """
        return data.fetch_error is None and not needs_session_nudge(data)

    def _within_cooldown(self) -> bool:
        """Return whether the previous attempt is still too recent to repeat."""
        if self._last_attempt_at is None:
            return False
        return self._clock() - self._last_attempt_at < self._cooldown_seconds

    def _record_failure(self) -> None:
        """Count one failed attempt and say so when it trips the breaker."""
        self._consecutive_failures += 1
        if self.exhausted:
            log.warning(
                "session refresh failed %s times — giving up until Claude usage "
                "recovers; sign in with `claude /login`",
                self._consecutive_failures,
            )

    def _nudge(self) -> None:
        """Run one CLI refresh and announce it, always reopening the gate after."""
        try:
            if self._invoke():
                self._consecutive_failures = 0
                self._on_refreshed()
            else:
                self._record_failure()
        except Exception:
            log.exception("session refresh failed")
            self._record_failure()
        finally:
            self._running = False
