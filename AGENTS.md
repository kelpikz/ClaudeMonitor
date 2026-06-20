# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Files & Folders

```
claudemonitor/
  main.py       — entry point; logging setup, single-instance mutex, poll loop, wires everything together
  fetcher.py    — calls Anthropic API; always returns AnthropicUsageData (errors included, never raises)
  processor.py  — pure function: AnthropicUsageData -> DisplayState; owns all string formatting
  tray.py       — drives pystray icon, tooltip, and menu; init() must be called before apply()
  models.py     — Pydantic models shared between layers: UsageWindow, AnthropicUsageData, DisplayState
  config.py     — reads/seeds %APPDATA%\claudemonitor\config.toml; exposes typed Config

run.py          — one-liner PyInstaller entry point (don't run directly; use uv run dev)
_scripts.py     — build script called by uv run build
design-v1.md    — full product + architecture spec for v1
docs/
  design-v1.md                       — full product + architecture spec for v1
  adr-0001-flat-module-architecture.md — ADR: why the flat 6-module layout was chosen
  project-info.md                    — project background and goals
  claudeNotes/                       — session notes written by Claude after each task (see Bookkeeping below)
```

## Commands

| Command        | What it does                                             |
| -------------- | -------------------------------------------------------- |
| `uv run dev`   | Run the tray app in the foreground with console output   |
| `uv run poll`  | Fetch from Anthropic API once, print JSON response, exit |
| `uv run build` | Build `dist/ClaudeMonitor.exe` via PyInstaller           |

## Debugging

Logs are written to `%APPDATA%\claudemonitor\claudemonitor.log` (rotating, 1 MB × 3 files).

Every successful fetch logs one INFO line: `fetched 5h=87% 7d=64%`. Errors log as WARNING. Unhandled poll-loop exceptions log as ERROR with a full traceback.

To open the log folder from the tray: right-click icon → **Open log folder**.

To check for orphan processes:
```bash
ps aux | grep -E "python|ClaudeMonitor" | grep -v grep
```

## Architecture notes

All architecture docs are in `docs/`. Start with `docs/design-v1.md` for the full spec — icon color thresholds, error mappings, tooltip format, deferred v2 features. `docs/adr-0001-flat-module-architecture.md` explains the structural decisions.

## Bookkeeping

At the end of every session or task, write a brief technical note to `./docs/claudeNotes/` named `YYYY-MM-DD-<short-slug>.md`. Include: what was changed, why, and any decisions or gotchas worth remembering. Keep it short — a future Claude should be able to scan it in 30 seconds.
