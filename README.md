<div align="center">
  <img src="assets/claudemonitor-hero.png" alt="ClaudeMonitor — Claude usage limits in the Windows taskbar" width="900" />
  <p>
    <a href="https://github.com/kelpikz/ClaudeMonitor/releases/latest">Download for Windows</a>
    ·
    <a href="#installation-windows">Installation</a>
    ·
    <a href="https://github.com/kelpikz/ClaudeMonitor/issues">Report an issue</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white" alt="Windows" />
    <img src="https://img.shields.io/github/license/kelpikz/ClaudeMonitor" alt="MIT license" />
    <img src="https://img.shields.io/badge/tests-685%20passing-2ea043" alt="685 tests passing" />
  </p>
</div>

ClaudeMonitor is a lightweight Windows system-tray app that monitors your **Claude** and **OpenAI Codex** usage limits at a glance. One tray icon covers both providers, coloured by whichever has the least **5-hour** usage left. Hover it to see each provider's 5-hour and 7-day numbers and when each window resets. The optional taskbar label shows one line per provider.

<table align="center">
  <tr>
    <td align="center" width="25%"><strong>📦 Portable</strong><br />Download one executable.<br />No installer or Python required.</td>
    <td align="center" width="25%"><strong>🔐 Uses your CLIs</strong><br />Reads the local sign-ins Claude Code<br />and Codex already created.</td>
    <td align="center" width="25%"><strong>⏱ Two providers</strong><br />Claude and Codex, 5-hour and<br />7-day windows, one line each.</td>
    <td align="center" width="25%"><strong>🧪 Tested</strong><br />685 automated tests<br />currently passing.</td>
  </tr>
</table>

## Preview

<p align="center">
  <img src="assets/claudemonitor-preview.png" alt="ClaudeMonitor showing Claude usage in the Windows taskbar" width="775" />
</p>

## Installation (Windows)

ClaudeMonitor is distributed as a portable Windows executable. It does not require Python, `uv`, or a separate installer.

1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) and sign in once. ClaudeMonitor reads the OAuth credentials created by Claude Code; it does not ask you to enter an API key.
   To track Codex as well, install the [Codex CLI](https://developers.openai.com/codex/cli) and run `codex login`. Codex tracking is on by default and can be switched off in the tray menu under **Settings…** → **Providers** (**Track Codex usage**), or in `config.toml`.
2. Download `ClaudeMonitor.exe` from the [latest GitHub release](https://github.com/kelpikz/ClaudeMonitor/releases/latest).
3. Place the executable in a permanent folder, such as `%LOCALAPPDATA%\ClaudeMonitor`, and double-click it.
4. Look for the ClaudeMonitor icon in the Windows notification area. Right-click it, open **Settings…**, and tick **Start with Windows** if you want it to launch automatically when you sign in.

   **Settings…** is a tabbed dialog. **General** holds startup, how often usage is fetched, and the percentages at which the icon turns amber and red; **Providers** holds Codex tracking, session auto-refresh, and links to each usage page; **Taskbar** holds the taskbar label. Changes are saved when you press **OK** or **Apply**, and **Cancel** discards them.

On first launch, the app creates its settings file at `%APPDATA%\claudemonitor\config.toml`. Logs are stored at `%APPDATA%\claudemonitor\claudemonitor.log`.

### Updating or uninstalling

To update, quit ClaudeMonitor, replace the executable with the newer release, and launch it again. Your settings and logs are kept in `%APPDATA%\claudemonitor`.

To uninstall, open **Settings…** → **General**, turn off **Start with Windows**, press **OK**, then quit the app. Then delete the executable. You can also delete `%APPDATA%\claudemonitor` to remove settings and logs.

If Windows SmartScreen warns about an unsigned release, verify that the executable came from the official GitHub release page before choosing **More info** → **Run anyway**.

## Development

Requires [uv](https://docs.astral.sh/uv/).

| Command        | What it does                                              |
| -------------- | -------------------------------------------------------- |
| `uv run dev`   | Run the tray app in the foreground with console output   |
| `uv run poll`  | Fetch both providers once, print the JSON, exit          |
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
