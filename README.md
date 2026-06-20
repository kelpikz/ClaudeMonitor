# ClaudeMonitor

A Windows system-tray app that monitors your Claude usage limits at a glance.

The tray icon changes color based on how much of your **5-hour** usage you have left. Hover the icon to see your current 5-hour and 7-day utilization and when each window resets. Right-click for actions like manual refresh and opening the log folder.

## Development

Requires [uv](https://docs.astral.sh/uv/).

| Command        | What it does                                              |
| -------------- | -------------------------------------------------------- |
| `uv run dev`   | Run the tray app in the foreground with console output   |
| `uv run poll`  | Fetch from the Anthropic API once, print the JSON, exit  |
| `uv run build` | Build `dist/ClaudeMonitor.exe` via PyInstaller           |

Logs are written to `%APPDATA%\claudemonitor\claudemonitor.log` (rotating, 1 MB × 3 files).

## Testing

Tests live in `tests/` and use [pytest](https://docs.pytest.org/).

```
uv run pytest
```

Run a single file or test:

```
uv run pytest tests/test_processor.py
uv run pytest tests/test_processor.py::TestProcessColors
```

## Running the built executable

After `uv run build`, launch the app:

```
dist\ClaudeMonitor.exe
```

The icon appears in the system tray (Windows taskbar notification area).
