"""Nudge a provider's CLI when the usage API says its session or token is idle.

The tray has no way to mint a fresh OAuth token or to open a usage window — only
the provider's own CLI can. Asking it for one cheap reply makes it do both as a
side effect: it refreshes an expired token before sending, and the reply itself
starts the usage window so a real reset countdown appears.

Both providers use the same ``SessionNudger`` and the same rule for when to
run it. The only difference between them is the command, which each
``Provider`` carries, so nothing in this module names a provider.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable

from .models import Provider, ProviderUsageData

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_COOLDOWN_SECONDS = 900 # 15 mins
MAX_CONSECUTIVE_FAILURES = 3

# A windowed build has no console, so an inherited one would flash on screen.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_NUDGEABLE_FETCH_ERRORS = frozenset({"token_expired"})


def needs_session_nudge(data: ProviderUsageData) -> bool:
    """Return whether this fetch describes a session the CLI could wake.

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
        return data.fetch_error in _NUDGEABLE_FETCH_ERRORS
    if data.five_hour is None:
        return False
    return data.five_hour.utilization <= 0.0


def _run_cli(
    *,
    executable_name: str,
    arguments: tuple[str, ...],
    which,
    run,
) -> bool:
    """Run one provider CLI prompt and report whether it answered.

    Never raises: this runs off the poll loop's thread, where an escaping error
    would be invisible in a windowed build.
    """
    executable = which(executable_name)
    if executable is None:
        log.warning(
            "%s CLI not found on PATH — skipping session refresh", executable_name
        )
        return False

    try:
        completed = run(
            [executable, *arguments],
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
        log.warning(
            "%s CLI did not answer within %ss", executable_name, COMMAND_TIMEOUT_SECONDS
        )
        return False
    except OSError as exc:
        log.warning("unable to launch %s CLI: %s", executable_name, exc)
        return False
    except Exception as exc:
        log.warning("unexpected error running %s CLI: %r", executable_name, exc)
        return False

    if completed.returncode != 0:
        log.warning(
            "%s CLI exited with %s: %s",
            executable_name,
            completed.returncode,
            (completed.stderr or "").strip(),
        )
        return False

    if not (completed.stdout or "").strip():
        log.warning(
            "%s CLI returned no output — session may not have started", executable_name
        )
        return False

    log.info("%s CLI answered — token and session refreshed", executable_name)
    return True


def run_provider_cli(
    provider: Provider,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Ask one provider's CLI for a throwaway reply, and say whether it answered.

    Which executable and which arguments are the provider's own, so there is
    no per-provider wrapper here: a third provider adds nothing to this file.
    """
    return _run_cli(
        executable_name=provider.cli_executable,
        arguments=provider.cli_arguments,
        which=which,
        run=run,
    )


def _start_daemon_thread(work: Callable[[], None]) -> None:
    """Run the CLI off the poll loop so the countdown keeps ticking meanwhile.
    
    We are running in a separate thread because, 
    even if it fails, nothing will happen to our main code
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
        enabled: bool = True,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        invoke: Callable[[], bool] | None = None,
        needs_nudge: Callable[[ProviderUsageData], bool] = needs_session_nudge,
        on_refreshed: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        start_background: Callable[[Callable[[], None]], None] = _start_daemon_thread,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self.provider = provider
        self._enabled = enabled
        self._cooldown_seconds = cooldown_seconds
        self._invoke = invoke or (lambda: run_provider_cli(provider))
        self._needs_nudge = needs_nudge
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

    def set_cooldown_seconds(self, seconds: float) -> None:
        """Change the shortest gap between nudges while the poll loop is running.

        The last attempt is deliberately kept, so shortening the cooldown lets
        the pending wait end early rather than starting it over.
        """
        self._cooldown_seconds = seconds

    # ENTRY POINT
    def maybe_nudge(self, data: ProviderUsageData) -> bool:
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

        if not self._enabled or self._running or not self._needs_nudge(data):
            return False
        if self.exhausted or self._within_cooldown():
            return False

        self._last_attempt_at = self._clock()
        self._running = True
        self._start_background(self._nudge)
        return True

    def _nothing_left_to_fix(self, data: ProviderUsageData) -> bool:
        """Return whether a healthy fetch shows there is nothing to nudge about.

        This is what re-arms the breaker. Re-arming on any successful fetch would
        be wrong: a missing CLI fails while fetches keep succeeding, and that
        would loop forever. Requiring live usage means the underlying problem is
        genuinely gone.
        """
        return data.fetch_error is None and not self._needs_nudge(data)

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
                "session refresh failed %s times — giving up until usage "
                "recovers; the provider CLI needs a manual sign-in",
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
