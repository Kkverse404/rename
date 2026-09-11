# Rename GUI (Windows / cross-platform)

A Qt-based tray + dashboard GUI for rename, built with **PySide6**.
Python 3.11 or newer is required, matching the CLI package.

Designed primarily for Windows (since the [Swift app](../macos-app/) is
macOS-only), but the same code runs on macOS and Linux too — useful if you
want a unified GUI across machines, or you don't want to build Swift on Mac.

## Features

- **System tray icon** with status indicator (running / paused / not installed),
  recent rename notifications (Windows balloons / macOS Notification Center),
  pause/resume daemon, open dashboard, show log, quit
- **Dashboard window** — stats cards (Tracked / Sessions / Stale / Renamed),
  brand-coloured tool filter chips (Claude / Codex / Cursor / Antigravity),
  search across titles and paths, per-session "Rename now" with bypass of the
  idle gate, before/after diff display, hover effects
- **Settings dialog** — visual editor for `~/.config/rename/config.toml`
  with spin boxes, dropdowns, and checkboxes. Saves back as TOML preserving
  comments
- **i18n** — English + 简体中文, auto-detected from system locale
  (override with `RENAME_GUI_LANG=en` or `RENAME_GUI_LANG=zh-Hans`)
- **Lazy loading** — session scanning only triggers on dashboard open or
  manual refresh, so the app doesn't hammer your AI session stores
  (which on Windows means fewer Defender/AntiVirus interruptions; on macOS
  fewer TCC prompts)
- **Friendly progress** — no raw `stderr`; all messages translated to
  human-readable toast notifications

## Install

```powershell
# Install rename first (the CLI is the source of truth; the GUI just talks to it)
pipx install rename-cli

# Then install the GUI
pipx install rename-gui
# or: pip install --user rename-gui

# Launch
rename-gui
```

Or from source:

```powershell
git clone https://github.com/study8677/rename.git
cd rename/windows-app
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate    # macOS / Linux
pip install -e .
rename-gui
```

## Daemon mode on Windows

There are two ways to keep the renamer running on Windows — pick whichever you
prefer. A machine-local lock prevents a second daemon from entering the loop.

1. **`rename install`** (recommended, set-and-forget) — registers a login
   **Startup** shortcut that launches `pythonw -m rename run` with no console
   window, and starts it immediately. It survives reboots, exactly like launchd
   on macOS or systemd on Linux. `rename uninstall` removes it and stops it.

2. **The tray app** (interactive control) — hit **Resume** and it spawns
   `rename run` as a child process with no console window and keeps it alive;
   **Pause** kills the child; closing the tray app stops it. Good when you want
   to start/stop by hand without a permanent install. A daemon started by
   `rename install` is shown as externally managed and must be stopped with
   `rename uninstall`; the tray does not kill an unrelated Python process.

Saved settings are reloaded before the daemon's next pass, including
structured mode and dry-run changes.

To also auto-launch the **GUI** itself at login, drop a shortcut to `rename-gui`
(or `pythonw -m rename_gui`) into:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

## Layout

```
windows-app/
├── pyproject.toml
└── rename_gui/
    ├── __main__.py        # entry point: `python -m rename_gui`
    ├── app.py             # QApp + tray + dashboard + settings + toasts
    ├── bridge.py          # subprocess wrapper around `rename ... --json`
    ├── config_store.py    # read/write ~/.config/rename/config.toml
    └── i18n.py            # en / zh-Hans translation dict
```

## Status

Windows CLI bridging, config round-trips, `.cmd` launching, and daemon locking
have automated coverage. A real interactive Windows GUI/notification smoke test
is still required before claiming full Desktop acceptance.
