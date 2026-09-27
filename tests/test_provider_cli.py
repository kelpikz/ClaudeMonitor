"""What each provider's CLI is asked, and how its answer is read.

The argv and the reply format are the two things the providers do not share:
Claude prints one JSON object, Codex prints one JSON event per line. The
bodies below are the shapes both CLIs actually printed, trimmed to the fields
that are read.
"""

from __future__ import annotations

import json

import pytest

from claudemonitor import codex_fetcher
from claudemonitor.models import CLAUDE, CODEX, PROVIDERS, CliReply


def _claude_result(**fields) -> str:
    """Build a `claude -p --output-format json` result object."""
    body = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Hi! How can I help?",
        "total_cost_usd": 0.0052,
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 15000,
            "output_tokens": 12,
        },
    }
    body.update(fields)
    return json.dumps(body)


def _codex_events(*events: dict) -> str:
    """Build `codex exec --json` output: one JSON object per line."""
    return "\n".join(json.dumps(event) for event in events)


_CODEX_STARTED = {"type": "thread.started", "thread_id": "01a0"}
_CODEX_TURN_STARTED = {"type": "turn.started"}
_CODEX_MESSAGE = {
    "type": "item.completed",
    "item": {"id": "item_0", "type": "agent_message", "text": "Hi!"},
}
_CODEX_COMPLETED = {
    "type": "turn.completed",
    "usage": {
        "input_tokens": 17405,
        "cached_input_tokens": 12032,
        "output_tokens": 13,
        "reasoning_output_tokens": 0,
    },
}
_MODEL_REJECTED = (
    '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-6-luna\' model is not supported when using Codex '
    'with a ChatGPT account."}}'
)


def _value_after(arguments: tuple[str, ...], flag: str) -> str:
    """Return the argument that follows one flag."""
    return arguments[arguments.index(flag) + 1]


def _codex_overrides(arguments: tuple[str, ...]) -> list[str]:
    """Return every `-c key=value` override Codex is given, in order."""
    return [arguments[i + 1] for i, part in enumerate(arguments) if part == "-c"]


class TestWhatClaudeIsAsked:
    def test_it_prints_one_json_result_for_a_one_word_prompt(self):
        arguments = CLAUDE.cli_arguments("haiku", "")

        assert arguments[:3] == ("-p", "--output-format", "json")
        assert arguments[-1] == "hi"

    def test_the_chosen_model_is_passed(self):
        arguments = CLAUDE.cli_arguments("haiku", "")

        assert _value_after(arguments, "--model") == "haiku"

    def test_the_chosen_effort_is_passed(self):
        arguments = CLAUDE.cli_arguments("haiku", "high")

        assert _value_after(arguments, "--effort") == "high"

    def test_a_blank_model_leaves_the_cli_s_own_default(self):
        assert "--model" not in CLAUDE.cli_arguments("", "")

    def test_a_blank_effort_asks_for_low(self):
        # A one-word reply needs no reasoning, whatever the user's own default is.
        assert _value_after(CLAUDE.cli_arguments("", ""), "--effort") == "low"

    def test_no_tools_are_offered(self):
        # Every tool definition is sent with the request; a nudge uses none.
        assert "--tools=" in CLAUDE.cli_arguments("", "")

    def test_no_mcp_servers_are_loaded(self):
        assert "--strict-mcp-config" in CLAUDE.cli_arguments("", "")

    def test_no_skills_are_listed(self):
        assert "--disable-slash-commands" in CLAUDE.cli_arguments("", "")

    def test_no_claude_md_or_user_settings_are_read(self):
        # An empty setting source list is what keeps the global and the
        # project CLAUDE.md out of the prompt. OAuth still works without it.
        assert "--setting-sources=" in CLAUDE.cli_arguments("", "")

    def test_the_long_default_system_prompt_is_replaced(self):
        arguments = CLAUDE.cli_arguments("", "")

        assert _value_after(arguments, "--system-prompt") == "Reply briefly."

    def test_the_run_is_kept_out_of_the_users_session_history(self):
        assert "--no-session-persistence" in CLAUDE.cli_arguments("", "")

    def test_bare_mode_is_never_used(self):
        # --bare never reads OAuth, so it could neither renew a token nor
        # start a subscription window.
        assert "--bare" not in CLAUDE.cli_arguments("", "")

    def test_no_argument_is_empty(self):
        # PowerShell 5.1 drops an empty argument, so a copied command with one
        # would not be the command the app runs.
        assert all(CLAUDE.cli_arguments("", ""))


@pytest.fixture
def no_codex_mcp_servers(monkeypatch):
    """Read no MCP server names, whatever the real ~/.codex/config.toml holds."""
    monkeypatch.setattr(codex_fetcher, "_configured_mcp_servers", lambda: ())


@pytest.mark.usefixtures("no_codex_mcp_servers")
class TestWhatCodexIsAsked:
    def test_it_runs_a_non_interactive_turn_that_prints_events(self):
        arguments = CODEX.cli_arguments("", "")

        assert arguments[:2] == ("exec", "--json")
        assert arguments[-1] == "hi"

    def test_it_refuses_to_touch_the_users_files(self):
        # The nudge only exists to make one authenticated request. A sandbox
        # that could write would let an off-hand model reply edit real files.
        assert _value_after(CODEX.cli_arguments("", ""), "--sandbox") == "read-only"

    def test_it_does_not_require_a_git_repository(self):
        # The tray app runs from wherever Windows launched it, which is usually
        # not a repository; without this the CLI refuses to start at all.
        assert "--skip-git-repo-check" in CODEX.cli_arguments("", "")

    def test_the_run_is_kept_out_of_the_users_session_history(self):
        assert "--ephemeral" in CODEX.cli_arguments("", "")

    def test_the_chosen_model_is_passed(self):
        assert _value_after(CODEX.cli_arguments("gpt-5.5", ""), "-m") == "gpt-5.5"

    def test_a_blank_model_leaves_the_users_own_codex_default(self):
        arguments = CODEX.cli_arguments("", "")

        assert "-m" not in arguments
        assert "--ignore-user-config" not in arguments

    def test_the_chosen_effort_overrides_the_users_codex_config(self):
        assert "model_reasoning_effort=high" in _codex_overrides(CODEX.cli_arguments("", "high"))

    def test_a_blank_effort_asks_for_low(self):
        # The user's own config.toml may ask for xhigh; a one-word nudge must
        # not inherit that.
        assert "model_reasoning_effort=low" in _codex_overrides(CODEX.cli_arguments("", ""))

    def test_the_long_base_instructions_are_replaced(self):
        # `instructions` is the key Codex reads; `base_instructions` is ignored.
        assert "instructions=Reply briefly." in _codex_overrides(CODEX.cli_arguments("", ""))

    def test_no_agents_md_is_read(self):
        assert "project_doc_max_bytes=0" in _codex_overrides(CODEX.cli_arguments("", ""))

    @pytest.mark.parametrize(
        "override",
        [
            "include_permissions_instructions=false",
            "include_apps_instructions=false",
            "include_environment_context=false",
            "include_collaboration_mode_instructions=false",
            "skills.include_instructions=false",
            "agents.enabled=false",
            "web_search=disabled",
            "notify=[]",
        ],
    )
    def test_each_unneeded_prompt_section_is_switched_off(self, override):
        assert override in _codex_overrides(CODEX.cli_arguments("", ""))

    @pytest.mark.parametrize(
        "feature",
        [
            "plugins",
            "apps",
            "hooks",
            "shell_tool",
            "unified_exec",
            "view_image",
            "multi_agent",
            "goals",
            "image_generation",
            "browser_use",
            "browser_use_external",
            "computer_use",
            "sleep_tool",
            "tool_suggest",
            "skill_search",
            "code_mode_host",
            "workspace_dependencies",
        ],
    )
    def test_each_tool_and_plugin_feature_is_switched_off(self, feature):
        assert f"features.{feature}=false" in _codex_overrides(CODEX.cli_arguments("", ""))

    def test_features_are_never_switched_off_with_the_disable_flag(self):
        # --disable stops Codex on a feature name it does not know, so one
        # feature removed by a Codex update would break every nudge.
        assert "--disable" not in CODEX.cli_arguments("", "")

    def test_no_override_value_carries_a_double_quote(self):
        # PowerShell 5.1 strips inner quotes, so a copied command with one would
        # not be the command the app runs. Codex reads an unparsable TOML value
        # as a plain string, which is what these are.
        assert not any('"' in value for value in _codex_overrides(CODEX.cli_arguments("", "")))

    def test_each_configured_mcp_server_is_switched_off(self, monkeypatch):
        # `mcp_servers={}` does not remove them; only a per-server switch does.
        monkeypatch.setattr(
            codex_fetcher, "_configured_mcp_servers", lambda: ("node_repl", "github")
        )

        overrides = _codex_overrides(CODEX.cli_arguments("", ""))

        assert "mcp_servers.node_repl.enabled=false" in overrides
        assert "mcp_servers.github.enabled=false" in overrides


class TestReadingTheCodexMcpServers:
    def test_every_server_named_in_the_config_is_found(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text(
            '[mcp_servers.node_repl]\ncommand = "node"\n\n'
            '[mcp_servers.github]\nurl = "https://example.com"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert codex_fetcher._configured_mcp_servers() == ("node_repl", "github")

    def test_a_missing_config_file_names_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert codex_fetcher._configured_mcp_servers() == ()

    def test_a_config_that_is_not_toml_names_none(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text("this is [not toml", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert codex_fetcher._configured_mcp_servers() == ()

    def test_a_config_without_servers_names_none(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert codex_fetcher._configured_mcp_servers() == ()

    def test_a_server_name_that_is_not_a_bare_key_is_skipped(self, tmp_path, monkeypatch):
        # A quoted key would need a double quote in the override, which
        # PowerShell 5.1 strips. Such a name is rare; it is left on.
        (tmp_path / "config.toml").write_text(
            '[mcp_servers."my server"]\ncommand = "x"\n', encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert codex_fetcher._configured_mcp_servers() == ()


class TestEveryProviderOffersEffortLevels:
    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_first_level_is_blank_which_asks_for_low(self, provider):
        assert provider.effort_levels[0] == ""

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_every_level_is_one_the_cli_accepts(self, provider):
        accepted = {"", "low", "medium", "high", "xhigh", "max"}

        assert set(provider.effort_levels) <= accepted


class TestReadingClaudesReply:
    def test_a_reply_counts_every_input_token_including_the_cache(self):
        reply = CLAUDE.read_cli_reply(0, _claude_result(), "")

        assert reply.succeeded is True
        assert (reply.input_tokens, reply.output_tokens) == (17003, 12)

    def test_a_reply_carries_the_text_claude_answered(self):
        reply = CLAUDE.read_cli_reply(0, _claude_result(), "")

        assert reply.reply_text == "Hi! How can I help?"

    def test_a_reply_carries_the_cost_claude_reported(self):
        reply = CLAUDE.read_cli_reply(0, _claude_result(), "")

        assert reply.cost_usd == 0.0052

    def test_a_missing_cost_is_not_invented(self):
        stdout = json.dumps({"type": "result", "is_error": False, "result": "Hi"})

        assert CLAUDE.read_cli_reply(0, stdout, "").cost_usd is None

    def test_a_cost_that_is_not_a_number_is_ignored(self):
        stdout = _claude_result(total_cost_usd="free")

        assert CLAUDE.read_cli_reply(0, stdout, "").cost_usd is None

    def test_an_api_error_carries_the_cli_s_own_sentence(self):
        stdout = _claude_result(
            is_error=True,
            result="There's an issue with the selected model (no-such-model).",
            usage={"input_tokens": 0, "output_tokens": 0},
        )

        reply = CLAUDE.read_cli_reply(1, stdout, "[claude-code:unrecognized_model]")

        assert reply.succeeded is False
        assert reply.detail == "There's an issue with the selected model (no-such-model)."

    def test_an_error_flag_fails_even_with_a_zero_exit_code(self):
        stdout = _claude_result(is_error=True, result="Credit balance is too low")

        assert CLAUDE.read_cli_reply(0, stdout, "").succeeded is False

    def test_a_non_zero_exit_without_json_uses_the_last_error_line(self):
        reply = CLAUDE.read_cli_reply(1, "", "starting\nInvalid API key\n")

        assert reply == CliReply(succeeded=False, detail="Invalid API key")

    def test_a_non_zero_exit_with_nothing_printed_names_the_exit_code(self):
        reply = CLAUDE.read_cli_reply(3, "", "")

        assert reply.succeeded is False
        assert "3" in reply.detail

    def test_output_that_is_not_json_is_not_a_success(self):
        reply = CLAUDE.read_cli_reply(0, "Hello!", "")

        assert reply.succeeded is False
        assert reply.detail

    def test_missing_usage_is_a_success_without_counts(self):
        stdout = json.dumps({"type": "result", "is_error": False, "result": "Hi"})

        reply = CLAUDE.read_cli_reply(0, stdout, "")

        assert reply == CliReply(succeeded=True, reply_text="Hi")


class TestReadingCodexsReply:
    def test_a_completed_turn_reports_its_tokens(self):
        stdout = _codex_events(
            _CODEX_STARTED, _CODEX_TURN_STARTED, _CODEX_MESSAGE, _CODEX_COMPLETED
        )

        reply = CODEX.read_cli_reply(0, stdout, "Reading additional input from stdin...")

        assert reply.succeeded is True
        assert (reply.input_tokens, reply.output_tokens) == (17405, 13)

    def test_a_completed_turn_carries_the_text_codex_answered(self):
        stdout = _codex_events(
            _CODEX_STARTED, _CODEX_TURN_STARTED, _CODEX_MESSAGE, _CODEX_COMPLETED
        )

        assert CODEX.read_cli_reply(0, stdout, "").reply_text == "Hi!"

    def test_an_item_without_text_does_not_hide_the_answer(self):
        # A reasoning item completes before the message and carries no text.
        reasoning = {"type": "item.completed", "item": {"type": "reasoning"}}
        stdout = _codex_events(_CODEX_MESSAGE, reasoning, _CODEX_COMPLETED)

        assert CODEX.read_cli_reply(0, stdout, "").reply_text == "Hi!"

    def test_codex_reports_no_cost(self):
        stdout = _codex_events(_CODEX_MESSAGE, _CODEX_COMPLETED)

        assert CODEX.read_cli_reply(0, stdout, "").cost_usd is None

    def test_a_rejected_model_is_named_in_plain_words(self):
        stdout = _codex_events(
            _CODEX_STARTED,
            _CODEX_TURN_STARTED,
            {"type": "error", "message": _MODEL_REJECTED},
            {"type": "turn.failed", "error": {"message": _MODEL_REJECTED}},
        )

        reply = CODEX.read_cli_reply(1, stdout, "")

        assert reply.succeeded is False
        assert reply.detail == (
            "The 'gpt-6-luna' model is not supported when using Codex "
            "with a ChatGPT account."
        )

    def test_an_error_message_that_is_plain_text_is_kept_as_it_is(self):
        stdout = _codex_events({"type": "error", "message": "stream disconnected"})

        assert CODEX.read_cli_reply(1, stdout, "").detail == "stream disconnected"

    def test_a_retry_notice_before_a_completed_turn_is_still_a_success(self):
        stdout = _codex_events(
            {"type": "error", "message": "Reconnecting... 1/5"},
            _CODEX_COMPLETED,
        )

        assert CODEX.read_cli_reply(0, stdout, "").succeeded is True

    def test_a_non_zero_exit_is_a_failure_even_after_a_completed_turn(self):
        stdout = _codex_events(_CODEX_COMPLETED)

        assert CODEX.read_cli_reply(1, stdout, "").succeeded is False

    def test_a_clean_exit_without_a_completed_turn_is_not_a_success(self):
        stdout = _codex_events(_CODEX_STARTED, _CODEX_TURN_STARTED)

        reply = CODEX.read_cli_reply(0, stdout, "")

        assert reply.succeeded is False
        assert reply.detail

    def test_lines_that_are_not_json_are_skipped(self):
        stdout = "warning: something\n" + _codex_events(_CODEX_COMPLETED)

        assert CODEX.read_cli_reply(0, stdout, "").succeeded is True

    def test_nothing_printed_falls_back_to_the_last_error_line(self):
        reply = CODEX.read_cli_reply(1, "", "Reading additional input\nNot logged in\n")

        assert reply == CliReply(succeeded=False, detail="Not logged in")
