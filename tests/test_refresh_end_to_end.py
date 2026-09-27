"""The session refresh, from the usage API's answer to the CLI run and its log line.

One end is the body the usage endpoint returned; the other is the argv handed
to the CLI and the line the log gets about how that run went. Between them run
the real fetcher, the real poller, the real nudger, the real CLI reader, and
the real settings model — only the network, the executable lookup, and the
child process are stood in for.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudemonitor import cli_refresher, codex_fetcher, main, usage_request
from claudemonitor.config import CodexConfig, Config, ConfigSetting
from claudemonitor.models import CLAUDE, CODEX, ProviderUsageData
from claudemonitor.settings import PendingSettings

_MODEL_REJECTED = (
    '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-6-luna\' model is not supported when using Codex '
    'with a ChatGPT account."}}'
)


@pytest.fixture(autouse=True)
def no_config_file(monkeypatch):
    """Keep every write off the real config file."""
    monkeypatch.setattr(ConfigSetting, "save", lambda self, value: None)


def _codex_body(used_percent: float | None) -> dict:
    """Build a body shaped like GET /backend-api/wham/usage."""
    window = {"used_percent": used_percent, "limit_window_seconds": 18000, "reset_at": None}
    return {
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": dict(window) if used_percent is not None else None,
            "secondary_window": dict(window, used_percent=50.0, limit_window_seconds=604800),
        },
    }


def _codex_events(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


_CODEX_SUCCESS = _codex_events(
    {"type": "thread.started", "thread_id": "01a0"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Hi!"}},
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 17405, "cached_input_tokens": 12032, "output_tokens": 13},
    },
)

_CODEX_REJECTED = _codex_events(
    {"type": "thread.started", "thread_id": "01a0"},
    {"type": "error", "message": _MODEL_REJECTED},
    {"type": "turn.failed", "error": {"message": _MODEL_REJECTED}},
)


class _Response:
    """Stand-in for httpx.Response covering only what the fetcher reads."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.headers: dict[str, str] = {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self) -> None:
        """Nothing to raise: every status here is a 200."""


class _Cli:
    """Stands in for the child process: records each argv, replays one output."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return self.result


class _Companion:
    visible = True
    healthy = True


class _CodexApp:
    """The production Codex path, over a canned API and a canned CLI."""

    def __init__(self, monkeypatch, cli: _Cli, config: Config | None = None, *, installed=True):
        self.responses: list[_Response] = []
        monkeypatch.setattr(
            codex_fetcher,
            "_read_credentials",
            lambda: codex_fetcher.CodexCredentials(
                access_token="test-token", account_id="acct", expires_at=None
            ),
        )
        monkeypatch.setattr(
            usage_request.httpx, "get", lambda *args, **kwargs: self.responses.pop(0)
        )
        monkeypatch.setattr(
            cli_refresher.shutil,
            "which",
            lambda name: f"C:/bin/{name}.CMD" if installed else None,
        )
        monkeypatch.setattr(cli_refresher, "run_cli_process", cli)
        self.cli = cli
        self.config = config or Config()
        self.woken = threading.Event()
        # The production pair, so the nudge and Run now share the real lock.
        self.nudger, self.manual_run = main.create_cli_runners(
            CODEX, self.config, self.woken, start_background=lambda work: work()
        )
        self.poller = main.ProviderPoller(CODEX, self.config, self.nudger)
        self.clipboard: list[str] = []
        self.changes: list[str] = []
        self.settings = main.build_settings_model(
            companion=_Companion(),
            pollers=[self.poller],
            config=self.config,
            log_dir=Path("."),
            manual_runs={"codex": self.manual_run},
            copy_text=lambda text: self.clipboard.append(text) or True,
            providers=(CODEX,),
        )

    def field(self, key: str):
        return next(field for field in self.settings.fields() if field.key == key)

    def click(self, key: str) -> None:
        """Click a command button, as the window does: on the values being edited."""
        pending = PendingSettings(self.settings)
        self.field(key).act(
            str(pending.value_of("codex_model")),
            str(pending.value_of("codex_effort")),
            lambda: self.changes.append("changed"),
        )

    def box(self) -> str:
        return self.field("codex_last_run").text()

    def poll(self, response: _Response) -> None:
        self.responses.append(response)
        self.poller.poll()


class TestAnIdleCodexWindow:
    """Happy path: an untouched window and a CLI that answers."""

    def test_the_run_is_logged_with_its_tokens(self, monkeypatch, caplog):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        with caplog.at_level(logging.INFO):
            app.poll(_Response(200, _codex_body(0.0)))

        assert "codex CLI answered" in caplog.text
        assert "17405 tokens in, 13 out" in caplog.text
        assert app.woken.is_set()

    def test_the_cli_is_asked_with_the_default_low_effort(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.poll(_Response(200, _codex_body(0.0)))

        command = app.cli.commands[0]
        assert command[:3] == ["C:/bin/codex.CMD", "exec", "--json"]
        assert "model_reasoning_effort=low" in command
        assert "-m" not in command

    def test_the_cli_is_asked_with_the_lean_command(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.poll(_Response(200, _codex_body(0.0)))

        assert app.cli.commands[0][1:] == list(CODEX.cli_arguments("", "low"))


class TestWhenTheRunFails:
    """Every failure reaches the log as a sentence, never as silence."""

    def test_a_rejected_model_is_named(self, monkeypatch, caplog):
        app = _CodexApp(monkeypatch, _Cli(returncode=1, stdout=_CODEX_REJECTED))

        with caplog.at_level(logging.WARNING):
            app.poll(_Response(200, _codex_body(0.0)))

        assert (
            "codex CLI refresh failed: The 'gpt-6-luna' model is not supported "
            "when using Codex with a ChatGPT account."
        ) in caplog.text
        assert not app.woken.is_set()

    def test_a_missing_cli_is_named(self, monkeypatch, caplog):
        app = _CodexApp(monkeypatch, _Cli(), installed=False)

        with caplog.at_level(logging.WARNING):
            app.poll(_Response(200, _codex_body(0.0)))

        assert "The codex CLI was not found on PATH." in caplog.text
        assert app.cli.commands == []

    def test_output_that_cannot_be_read_is_a_failure(self, monkeypatch, caplog):
        app = _CodexApp(monkeypatch, _Cli(returncode=0, stdout="not json at all"))

        with caplog.at_level(logging.WARNING):
            app.poll(_Response(200, _codex_body(0.0)))

        assert "codex CLI refresh failed" in caplog.text
        assert not app.woken.is_set()

    def test_three_failures_stop_the_retries(self, monkeypatch):
        config = Config(codex=CodexConfig(cooldown_seconds=0))
        app = _CodexApp(monkeypatch, _Cli(returncode=1, stdout=_CODEX_REJECTED), config)

        for _ in range(5):
            app.poll(_Response(200, _codex_body(0.0)))

        assert len(app.cli.commands) == 3


class TestARunThatFixesNothing:
    """A CLI that answers every time, and a fetch that never changes. Without a
    limit, each poll past the cooldown would spend another run for ever."""

    def test_a_window_that_stays_at_zero_stops_after_three_runs(self, monkeypatch):
        config = Config(codex=CodexConfig(cooldown_seconds=0))
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS), config)

        for _ in range(10):
            app.poll(_Response(200, _codex_body(0.0)))

        assert len(app.cli.commands) == 3
        assert app.nudger.exhausted is True

    def test_a_refusal_that_is_not_about_the_token_stops_after_three_runs(
        self, monkeypatch
    ):
        config = Config(codex=CodexConfig(cooldown_seconds=0))
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS), config)

        for _ in range(10):
            app.poll(_Response(403, {}))

        assert len(app.cli.commands) == 3

    def test_a_run_that_starts_the_window_is_not_counted_against_it(self, monkeypatch):
        config = Config(codex=CodexConfig(cooldown_seconds=0))
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS), config)

        for _ in range(5):
            app.poll(_Response(200, _codex_body(0.0)))
            app.poll(_Response(200, _codex_body(3.0)))

        assert len(app.cli.commands) == 5
        assert app.nudger.exhausted is False


class TestWhenTheApiAnswerIsNoUse:
    """Nothing is run on an answer that says nothing about the session."""

    def test_an_offline_api_runs_nothing(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        monkeypatch.setattr(
            usage_request.httpx,
            "get",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                usage_request.httpx.ConnectError("offline")
            ),
        )

        app.poller.poll()

        assert app.cli.commands == []

    def test_a_body_without_a_five_hour_window_runs_nothing(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.poll(_Response(200, _codex_body(None)))

        assert app.cli.commands == []

    def test_a_body_that_is_not_json_runs_nothing(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.poll(_Response(200, ValueError("not json")))

        assert app.cli.commands == []

    def test_a_window_in_use_runs_nothing(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.poll(_Response(200, _codex_body(42.0)))

        assert app.cli.commands == []


class TestWhatTheUserChoosesInSettings:
    """An Apply in the window changes the very next run."""

    def test_a_model_and_effort_applied_in_the_window_reach_the_cli(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        pending = PendingSettings(app.settings)
        pending.edit("codex_model", "gpt-5.5")
        pending.edit("codex_effort", "medium")
        pending.apply()

        app.poll(_Response(200, _codex_body(0.0)))

        command = app.cli.commands[0]
        assert command[command.index("-m") + 1] == "gpt-5.5"
        assert "model_reasoning_effort=medium" in command

    def test_waking_switched_off_in_the_window_runs_nothing(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        pending = PendingSettings(app.settings)
        pending.edit("codex_wake_session", False)
        pending.apply()

        app.poll(_Response(200, _codex_body(0.0)))

        assert app.cli.commands == []

    def test_a_shorter_cooldown_applied_in_the_window_allows_the_next_run(
        self, monkeypatch
    ):
        clock = [0.0]
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        app.nudger._clock = lambda: clock[0]
        app.poll(_Response(200, _codex_body(0.0)))
        clock[0] = 120.0

        pending = PendingSettings(app.settings)
        pending.edit("codex_cooldown", 60)
        pending.apply()
        app.poll(_Response(200, _codex_body(0.0)))

        assert len(app.cli.commands) == 2


class TestAnExpiredClaudeToken:
    """The same path for Claude, whose CLI prints one JSON object."""

    def test_a_renewal_runs_haiku_and_logs_its_tokens(self, monkeypatch, caplog):
        answer = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "Hi!",
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 4,
                },
            }
        )
        cli = _Cli(stdout=answer)
        monkeypatch.setattr(cli_refresher.shutil, "which", lambda name: f"C:/bin/{name}.EXE")
        monkeypatch.setattr(cli_refresher, "run_cli_process", cli)
        expired = ProviderUsageData(
            fetch_error="token_expired", fetched_at=datetime.now(timezone.utc)
        )
        provider = replace(CLAUDE, fetch=lambda: expired)
        config = Config()
        nudger = main.create_session_nudger(
            provider, config, threading.Event(), start_background=lambda work: work()
        )

        with caplog.at_level(logging.INFO):
            main.ProviderPoller(provider, config, nudger).poll()

        assert "claude CLI answered" in caplog.text
        assert "1003 tokens in, 4 out" in caplog.text
        command = cli.commands[0]
        assert command[command.index("--model") + 1] == "haiku"


class TestRunNowInSettings:
    """From Run now, through the real CLI runner and reader, to the text in the box."""

    def test_a_success_shows_the_reply_the_tokens_and_the_command(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.click("codex_run_command")

        lines = app.box().split("\n")
        assert lines[0].startswith("Succeeded in ")
        assert lines[1:4] == [
            "Reply: Hi!",
            "Tokens: 17,405 in, 13 out",
            "Cost: not reported by the Codex CLI",
        ]
        assert lines[-1] == cli_refresher.command_line(CODEX, "", "low")

    def test_the_cli_is_run_with_the_settings_shown(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.click("codex_run_command")

        assert app.cli.commands[0][1:] == list(CODEX.cli_arguments("", "low"))

    def test_a_success_asks_for_fresh_usage(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.click("codex_run_command")

        assert app.woken.is_set()

    def test_the_window_is_told_when_the_run_starts_and_ends(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.click("codex_run_command")

        assert app.changes == ["changed", "changed"]

    def test_a_rejected_model_is_shown_in_plain_words(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(returncode=1, stdout=_CODEX_REJECTED))

        app.click("codex_run_command")

        lines = app.box().split("\n")
        assert lines[0].startswith("Failed after ")
        assert lines[1] == (
            "The 'gpt-6-luna' model is not supported when using Codex "
            "with a ChatGPT account."
        )
        assert not app.woken.is_set()

    def test_a_missing_cli_is_shown(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(), installed=False)

        app.click("codex_run_command")

        assert app.box().startswith("Failed.\nThe codex CLI was not found on PATH.")
        assert app.cli.commands == []

    def test_output_that_cannot_be_read_is_shown_as_a_failure(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(returncode=0, stdout="not json at all"))

        app.click("codex_run_command")

        assert app.box().startswith("Failed after ")

    def test_a_run_ignores_the_cooldown_and_the_switches(self, monkeypatch):
        config = Config(codex=CodexConfig(wake_session=False, renew_token=False))
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS), config)

        app.click("codex_run_command")

        assert len(app.cli.commands) == 1

    def test_copy_puts_the_same_command_on_the_clipboard(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))

        app.click("codex_copy_command")

        assert app.clipboard == [cli_refresher.command_line(CODEX, "", "low")]
        assert app.box() == f"Command copied to the clipboard:\n{app.clipboard[0]}"
        assert app.cli.commands == []

    def test_the_copied_command_is_the_argv_the_app_runs(self, monkeypatch):
        # The user who pastes the line runs what the app runs.
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        app.click("codex_copy_command")
        app.click("codex_run_command")

        assert app.clipboard[0] == subprocess.list2cmdline(["codex", *app.cli.commands[0][1:]])

    def test_a_model_typed_and_applied_is_the_one_run(self, monkeypatch):
        app = _CodexApp(monkeypatch, _Cli(stdout=_CODEX_SUCCESS))
        pending = PendingSettings(app.settings)
        pending.edit("codex_model", "gpt-5.5")
        pending.apply()

        app.click("codex_run_command")

        command = app.cli.commands[0]
        assert command[command.index("-m") + 1] == "gpt-5.5"
        assert "Command:" in app.box()


class TestRunNowForClaude:
    """The same path for Claude, which also reports a cost."""

    def test_a_success_shows_the_cost_claude_reported(self, monkeypatch):
        answer = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": "Hi!",
                "total_cost_usd": 0.005188,
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 635,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                },
            }
        )
        monkeypatch.setattr(cli_refresher.shutil, "which", lambda name: f"C:/bin/{name}.EXE")
        monkeypatch.setattr(cli_refresher, "run_cli_process", _Cli(stdout=answer))
        run = cli_refresher.ManualRun(CLAUDE, start_background=lambda work: work())
        settings = main.build_settings_model(
            companion=_Companion(),
            pollers=[],
            config=Config(),
            log_dir=Path("."),
            manual_runs={"claude": run},
            providers=(CLAUDE,),
        )
        fields = {field.key: field for field in settings.fields()}

        fields["claude_run_command"].act("haiku", "", lambda: None)

        lines = fields["claude_last_run"].text().split("\n")
        assert lines[1:4] == ["Reply: Hi!", "Tokens: 637 in, 5 out", "Cost: $0.0052"]
