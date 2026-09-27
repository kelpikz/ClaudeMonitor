"""Settings shared by every test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def empty_codex_home(monkeypatch, tmp_path):
    """Point Codex at an empty folder, so no test reads this machine's ~/.codex.

    The Codex command switches off each MCP server the user's config names, so
    without this the argv a test sees would depend on whoever runs it. A test
    about that config sets CODEX_HOME to a folder of its own.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
