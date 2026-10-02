"""What a Provider has to carry, for every provider the application knows.

Everything that used to be a wrapper function per provider — which CLI to run,
which fetcher answers, which page the settings link opens, which setting turns
it off — is a field here now. These tests are what makes "nothing else branches
on which provider it is" checkable rather than a claim in a comment.
"""

from __future__ import annotations

import pytest

from claudemonitor import codex_fetcher, config, fetcher, icon_art, label_art, processor
from claudemonitor.models import CLAUDE, CODEX, PROVIDERS, ProviderUsageData


class TestEveryProviderIsComplete:
    """A provider missing a piece must fail here, not in front of the user."""

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_it_names_itself(self, provider):
        assert provider.key
        assert provider.label

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_it_has_a_usage_page_to_link_to(self, provider):
        assert provider.usage_url.startswith("https://")

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_it_names_the_cli_that_can_renew_its_token(self, provider):
        assert provider.cli_executable
        assert provider.cli_arguments("", "")
        assert callable(provider.read_cli_reply)

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_it_has_sentences_written_about_it(self, provider):
        # A missing entry raises rather than borrowing Claude's wording, which
        # would tell a Codex user to start Claude Code.
        assert processor._WORDING[provider.key].heading

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_it_has_a_mark_of_its_own(self, provider):
        assert provider.key in icon_art._GLYPH_MASKS
        assert provider.key in label_art._GLYPH_COLORS

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_its_mark_is_not_another_providers(self, provider):
        others = [other for other in PROVIDERS if other is not provider]
        mine = label_art.taskbar_glyph(provider, 20, uses_light_theme=False)

        for other in others:
            theirs = label_art.taskbar_glyph(other, 20, uses_light_theme=False)
            assert mine.tobytes() != theirs.tobytes()

    def test_every_key_is_used_once(self):
        assert len({provider.key for provider in PROVIDERS}) == len(PROVIDERS)


class TestWhichFetcherAnswers:
    """The fetcher is the provider's own, so nothing keeps a table of them."""

    def test_claude_reaches_its_own_fetcher(self, monkeypatch):
        answered = ProviderUsageData(fetched_at=_any_time())
        monkeypatch.setattr(fetcher, "fetch", lambda: answered)

        assert CLAUDE.fetch() is answered

    def test_codex_reaches_its_own_fetcher(self, monkeypatch):
        answered = ProviderUsageData(fetched_at=_any_time())
        monkeypatch.setattr(codex_fetcher, "fetch", lambda: answered)

        assert CODEX.fetch() is answered

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_the_import_is_deferred_but_resolvable(self, provider):
        # The shim imports inside the call because the fetchers import this
        # module for their return type. A typo in the module name would only
        # show up at the first poll, so it is resolved here instead.
        assert callable(provider.fetch)


class TestEveryProviderCanBeSwitchedOff:
    """Each provider owns one config section, named by its own key."""

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_its_settings_live_in_the_section_named_by_its_key(self, provider):
        assert {setting.section for setting in provider.settings.every()} == {
            provider.key
        }

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_its_settings_are_ones_the_config_really_has(self, provider):
        for setting in provider.settings.every():
            assert setting in config.EVERY_SETTING

    def test_claude_can_be_switched_off_as_codex_can(self):
        assert CLAUDE.settings.tracking == config.ConfigSetting("claude", "enabled")
        assert CODEX.settings.tracking == config.ConfigSetting("codex", "enabled")


def _any_time():
    """A timestamp for a fetch result nothing in these tests reads."""
    from datetime import datetime, timezone

    return datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
