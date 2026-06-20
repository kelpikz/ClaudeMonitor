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

tests/          — pytest suite (run via uv run pytest)
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
| `uv run pytest`| Run the test suite in `tests/`                           |

## Development process

### Development method
- Follow a red green TDD (Test Driven Development) mode of architecture. 
- You should always write / modify tests first. And verify if everything the tests are failing before starting to implment the feature
- There are 2 types of tests which you  should write. 
  - 1. Test for the whole feature - This test should test every possible case for the given end to end path. When I say "end to end path" I mean, the it is from one end "data received from the api" to the other end "what is shown to the user". It should first test the happy path and then edge cases like api failure or wrong format. 
  - 2. Unit tests for each function which is created. 
- After implementing you should make sure the the tests pass.
- 

### Code Structure
- The code written should be modular and easy to test. Every function should do one thing and one thing only
- Variable and funciton names should be descriptive and every function should contain a comment on what it does


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
