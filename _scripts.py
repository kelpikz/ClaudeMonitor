from pathlib import Path
from tempfile import TemporaryDirectory

import PyInstaller.__main__

from claudemonitor.icon_art import tile_icon


_APPLICATION_ICON_COLOR = (46, 160, 67)
_APPLICATION_ICON_SIZES = [
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
]

_UNUSED_PILLOW_MODULES = (
    "PIL._avif",
    "PIL._imagingcms",
    "PIL._imagingft",
    "PIL._imagingmath",
    "PIL._imagingtk",
    "PIL._webp",
)


def _write_application_icon(path: Path) -> None:
    """Render the ClaudeMonitor artwork as a multi-size Windows icon."""
    largest_size = max(width for width, _height in _APPLICATION_ICON_SIZES)
    tile_icon(_APPLICATION_ICON_COLOR, size=largest_size).save(
        path,
        format="ICO",
        sizes=_APPLICATION_ICON_SIZES,
    )


def _pyinstaller_options(icon_path: Path) -> list[str]:
    """Return the PyInstaller options needed for the portable application."""
    options = [
        "--onefile",
        "--windowed",
        "--name", "ClaudeMonitor",
        "--icon", str(icon_path),
        "--collect-submodules", "claudemonitor",
        "--add-data", "claudemonitor/assets;claudemonitor/assets",
    ]
    for module in _UNUSED_PILLOW_MODULES:
        options.extend(["--exclude-module", module])
    options.append("run.py")
    return options


def build() -> None:
    """Build the portable executable with generated application artwork."""
    with TemporaryDirectory(prefix="claudemonitor-build-") as temporary_directory:
        icon_path = Path(temporary_directory) / "ClaudeMonitor.ico"
        _write_application_icon(icon_path)
        PyInstaller.__main__.run(_pyinstaller_options(icon_path))
