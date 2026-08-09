from pathlib import Path

from _scripts import _pyinstaller_options


def test_build_excludes_unused_pillow_codecs():
    """The bundle should not carry Pillow codecs the app never uses."""
    options = _pyinstaller_options(Path("ClaudeMonitor.ico"))

    excluded_modules = {
        options[index + 1]
        for index, option in enumerate(options[:-1])
        if option == "--exclude-module"
    }

    assert {
        "PIL._avif",
        "PIL._imagingcms",
        "PIL._imagingft",
        "PIL._imagingmath",
        "PIL._imagingtk",
        "PIL._webp",
    } <= excluded_modules

