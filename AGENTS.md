# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Files & Folders

```
claudemonitor/
  main.py                 — entry point; logging setup, single-instance mutex, poll loop, wires everything together
  usage_request.py        — the one HTTP usage request both providers make, and the single ladder of
                            ways it fails; nothing raises across this boundary
  fetcher.py              — what is Claude's own: its credentials file, its URL, its body shape
  codex_fetcher.py        — the same for Codex: ~/.codex/auth.json, the ChatGPT usage endpoint
  processor.py            — pure functions: ProviderUsageData -> DisplayState, plus the combining of
                            those states into one TrayState and one TaskbarLabel; owns all formatting,
                            including the one row per FetchError that every surface reads
  tray.py                 — drives the one pystray icon; init() must be called before apply()
  icon_art.py             — pure Pillow drawing of the tray status tiles (no pystray, no Windows state)
  label_art.py            — pure Pillow composition of the taskbar label's bitmap: which mark a
                            provider gets, the room around a stacked row, and the bytes Windows reads
  models.py               — shared cross-layer types: Provider (and the CLAUDE/CODEX constants),
                            FetchError, UsageWindow, ProviderUsageData, DisplayState, TrayState,
                            LabelSegment, TaskbarLabel, Rect
  config.py               — reads/seeds %APPDATA%\claudemonitor\config.toml; exposes typed Config and
                            one ConfigSetting per writable value (read it, change it, save it)
  notifications.py        — decides when a threshold crossing warrants a desktop notification
  cli_refresher.py        — runs a provider's CLI to wake an idle session or an expired token
  settings.py             — the tabs, groups, and fields the settings window contains, the buffer that
                            holds an edit until Apply, and the one-window-at-a-time controller
  settings_layout.py      — where every control in that window sits; pure geometry, no user32
  taskbar_companion.py    — controller for the taskbar usage label: owns its UI thread and placement maths
  win32_taskbar_window.py — user32/gdi32 for the taskbar label; implements the NativeWindow protocol.
                            Rasterises the text and hands Windows one finished bitmap (see below)
  win32_settings_window.py — user32/gdi32 for the settings dialog; implements the SettingsView protocol
  win32_dpi.py            — this process's DPI awareness, and what a 96-DPI constant is worth on it
  win32_text.py           — the system UI font and text measurement, shared by both windows
  win32_bindings.py       — Windows constants, C structs, and function signature tables (no behavior)

tests/          — pytest suite (run via uv run pytest)
tools/          — extract_codex_glyph.py: pulls the OpenAI mark into the committed mask.
                  Every size and tone is drawn from that mask at run time
run.py          — one-liner PyInstaller entry point (don't run directly; use uv run dev)
_scripts.py     — build script called by uv run build
docs/
  design-v1.md                       — full product + architecture spec for v1
  adr-0001-flat-module-architecture.md — ADR: why the flat module layout was chosen
  project-info.md                    — project background and goals
  agentNotes/                        — session notes written after each task (see Bookkeeping below)
```

## Commands

| Command        | What it does                                             |
| -------------- | -------------------------------------------------------- |
| `uv run dev`   | Run the tray app in the foreground with console output   |
| `uv run poll`  | Fetch both providers once, print JSON responses, exit    |
| `uv run build` | Build `dist/ClaudeMonitor.exe` via PyInstaller           |
| `uv run pytest`| Run the test suite in `tests/`                           |

## Development process

### Running the app
- Do not start `uv run dev`, the built executable, or any long-running Claude Monitor process unless the user explicitly asks. The user will normally launch the app themselves to inspect it.

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
- Prefer self explanatory code over comments


## Debugging

Logs are written to `%APPDATA%\claudemonitor\claudemonitor.log` (rotating, 1 MB × 3 files).

Every successful fetch logs one INFO line naming its provider: `fetched claude 5h=87% 7d=64%`,
`fetched codex 5h=12% 7d=64%`. Errors log as WARNING, and the provider is named there too. Unhandled
poll-loop exceptions log as ERROR with a full traceback.

To open the log folder from the tray: right-click icon → **Open log folder**.

To check for orphan processes:
```bash
ps aux | grep -E "python|ClaudeMonitor" | grep -v grep
```

## Architecture notes

All architecture docs are in `docs/`. Start with `docs/design-v1.md` for the full spec — icon color thresholds, error mappings, tooltip format, deferred v2 features. `docs/adr-0001-flat-module-architecture.md` explains the structural decisions. `docs/codex-integration.md` covers the second provider: the endpoint, its response shape, and how it maps onto the same modules.

### Two providers

Claude and Codex share every module. Everything that varies is a field on `models.Provider`: its key
and label, its usage URL, the CLI that renews its token and the argv to run, the fetcher that answers
for it, and the `ConfigSetting` that switches it off (`None` for Claude, which is always tracked).
Nothing else in the application branches on which provider it is holding, and no function names one.

Adding a third provider is therefore:

1. a fetcher module — the credentials, the URL, and the body shape; the request itself is shared;
2. a `Provider` constant in `models.py`, plus the small deferred-import shim that calls its fetcher
   (deferred because the fetcher imports `models` for its return type);
3. a `_WORDING` entry in `processor.py` — every sentence about a provider is written there;
4. a glyph: a mask builder in `icon_art._GLYPH_MASKS` and a tone pair in `label_art._GLYPH_COLORS`;
5. and, only if the user may switch it off, a `[section]` in the seeded config with a `ConfigSetting`.

Each of those refuses to be forgotten: a missing wording, glyph, or tone raises rather than quietly
borrowing another provider's.

Both providers are judged by one nudge rule in `cli_refresher.py`: an expired token, or an untouched
five-hour window. Codex briefly had a rule of its own, and the result was that an idle Codex window —
the one state a single message repairs — was never woken.

### One row per failure

`models.FetchError` is a closed set, and `processor._ERROR_DISPLAY` gives each value one row holding
all three things a failure is shown as: the short taskbar label, the menu status prefix, and the
tooltip sentence. These were three tables written in the same order, and a value present in two of
them but missing from the third fell through to wording that fitted a different failure. A fetcher
that meets something it cannot name writes `unknown` and puts the detail in the log.

### One name per setting

A writable setting is one `config.ConfigSetting(section, key)`. The TOML section names and the
`Config` attribute names are the same word, so that one pair reads the setting, changes it in the
running application, and writes it back. `save` logs rather than raises: every caller runs on a UI
thread and has already changed something the user can see, so a file that cannot be written must not
undo that. The section and the key used to be spelled twice for every setting — once in a `save_*`
wrapper, once as an attribute string in `main` — and the write passed through four layers.

### One icon, one label

One tray icon speaks for every provider. `processor.tray_status()` condenses the display states into a
`TrayState`: the most severe colour, one tooltip line per provider, one menu status line per provider.
The taskbar label is a single window that draws one `LabelSegment` per provider, stacked a row each, so
a second provider costs height rather than width.

### How the taskbar label is drawn

The label is a **per-pixel alpha** window: `UpdateLayeredWindow` is handed one
finished bitmap, and Windows blends it with whatever the taskbar shows behind
it. It is not painted through its own device context, so `WM_PAINT` only
composes a new bitmap and then validates the window.

This replaced a colour-key window, where every pixel painted pure black became a
hole. A layered window can honour a colour key or an alpha channel, never both,
and the marks are thin enough to be almost entirely anti-aliased edge — so
flattening them onto black ringed each one in black and dulled the whole mark.
The taskbar is rarely black: with transparency effects on it takes its tint from
the wallpaper, so no fixed colour could have been guessed either.

Consequences worth knowing:

- **GDI never writes alpha.** The text is rasterised in white on a cleared
  surface and the brightness that comes back is read as coverage
  (`label_art.coverage_mask`); the theme's colour is applied to that coverage
  afterwards. The font asks for `ANTIALIASED_QUALITY`, because ClearType tints
  each edge for one assumed background.
- **Marks are drawn, never stretched.** `label_art.taskbar_glyph` renders each
  one at the exact size its row leaves, cached per size.
- **Only masks are ever resized.** Pillow resamples an RGBA image with the alpha
  weighted in, which drifts a flat colour by up to a tenth at a soft edge.
  `icon_art` therefore downscales an `L` mask and paints the colour onto it.

### Where a setting lives

The tray menu keeps only what a user reaches for mid-task: the status lines, Refresh now, the taskbar
toggle, Settings…, Quit. Every other switch, number, and link is in the settings window, which runs on
a thread of its own because pystray owns the thread a menu click arrives on. That window is three
modules: `settings.py` says what it contains, `settings_layout.py` works out where each of those
things sits, and `win32_settings_window.py` is the only one that calls user32. The first two are
tested without a desktop.

The window is a tabbed dialog — General, Providers, Taskbar — of captioned group boxes, with
OK / Cancel / Apply along the bottom. **Nothing is written as it is clicked.** Every change goes into
a `PendingSettings` buffer and reaches the real setting only on OK or Apply, which is what lets
Cancel mean cancel. Each write changes the running application as well as `config.toml`: a new poll
interval resets every `ProviderPoller`, a new cooldown reaches every `SessionNudger`, and a new
threshold is followed by a wake-up so the icon redraws at once.

### What the settings dialog fights with Windows about

Four arrangements are load-bearing, and each one was a visible bug first:

- **Each tab page is a window, not a set of loose controls.** Laying every page's controls onto the
  frame and hiding the inactive ones looks the same until the first tab change, when whatever the
  hidden controls last drew stays printed on the page. A hidden control cannot repaint the pixels it
  owns, and no invalidation of the frame or of a group box reaches them.
- **A page must not have `WS_CLIPCHILDREN`.** A themed group box or checkbox paints no background of
  its own — it asks its parent to paint one. With the children clipped out of the page's erase, the
  frame's grey shows through the middle of every group box.
- **A child created later goes to the *bottom* of the z-order.** So a group box is created after the
  fields it surrounds, and the tab control is pushed to the back once its pages exist.
- **A group's caption is a static of ours, not the group box's title.** A group box paints its title
  in whichever colour the visual style picks, which on a dark page is black on near-black.
- **A field is routed by its position, not by its identity.** Two fields wired the same way are equal
  frozen dataclasses, so searching the field list for one of them would find the first for both. The
  index a field is created under is carried through to the control it gets.
- **The tab strip is painted by hand in dark mode.** `SysTabControl32` has no dark rendering and
  ignores `DarkMode_Explorer`, so left alone it draws a white strip and a white line round the page
  on an otherwise near-black dialog. The control is subclassed and answers `WM_PAINT` here; it still
  lays the strip out, hit-tests it, and reports a change of tab, so only the pixels are ours. Light
  mode is left native, because there Windows draws the better strip.

## Bookkeeping

At the end of every session or task, write a brief technical note to `./docs/agentNotes/` named `YYYY-MM-DD-<short-slug>.md`. Include: what was changed, why, and any decisions or gotchas worth remembering. Keep it short — a future Claude should be able to scan it in 30 seconds.
