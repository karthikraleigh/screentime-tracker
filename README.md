# Screen Time Tracker

A tiny, local-only screen time tracker for a Windows PC. It records which
application is in the foreground and logs the time (skipping idle time),
then shows a dashboard in your browser. No accounts, no cloud, no
third-party libraries — everything stays on the machine, and the web
server only listens on `127.0.0.1` (not reachable from the network).

## What it tracks

**App usage:** every 5 seconds it checks the active (foreground) window
and adds 5 seconds to that app's total for the day — unless the PC has
been idle (no mouse/keyboard input) for 60+ seconds, in which case it
doesn't count.

**Firefox websites:** every 20 seconds it reads Firefox's own local
history file (`places.sqlite` in the Firefox profile folder) and
records any new pages visited today — domain, page title, and time.
It works by copying that file (Firefox keeps it open while running) and
reading the copy, the same non-invasive approach browser-history tools
generally use; no extension or debugging protocol is needed.

Data is stored in `screentime.db` (SQLite) in this folder.

## Requirements

- Windows 10/11
- Python 3.8+ ([python.org/downloads](https://www.python.org/downloads/)) —
  during install, check **"Add python.exe to PATH"**.

No `pip install` needed — it only uses the Python standard library.

## Running it manually

```
python tracker_server.py
```

Then open **http://127.0.0.1:5757** in a browser. Leave the window open
(or minimize it) while the PC is being used.

## Running it automatically at login (recommended)

Use this on the **child's Windows user account** so it starts tracking
every time they log in, without needing a console window open.

1. Press `Win + R`, type `shell:startup`, press Enter — this opens the
   Startup folder for the current user.
2. Right-click inside that folder → **New → Shortcut**.
3. For the location, browse to `start_tracker.vbs` in this project
   folder, then finish the wizard.
4. Log off and back on (or just double-click the shortcut) — the
   tracker now runs silently in the background, and the dashboard is at
   http://127.0.0.1:5757 any time.

To stop it, open Task Manager and end the `pythonw.exe` process.

### More robust option: Task Scheduler

If you want it to start even without a full interactive logon (e.g. if
you use fast user switching), create a Task Scheduler task instead:

1. Open **Task Scheduler** → **Create Task**.
2. **General** tab: name it "Screen Time Tracker", and under the
   account, select the child's user account.
3. **Triggers** tab: New → "At log on" → specific user → the child's
   account.
4. **Actions** tab: New → Program: `wscript.exe`, Arguments: the full
   path to `start_tracker.vbs` in quotes.
5. Save.

## Settings

Edit `config.json` (created automatically on first run) to change:

```json
{
  "poll_interval_seconds": 5,
  "idle_threshold_seconds": 60,
  "daily_limit_minutes": 120,
  "firefox_history_scan_seconds": 20,
  "port": 5757
}
```

- `daily_limit_minutes` only affects the progress bar color on the
  dashboard (green/yellow/red) — it's informational, not an enforced
  block.
- Restart the tracker after changing settings.

## Viewing the dashboard remotely on your own device

The server only binds to `127.0.0.1`, so it's only visible on the same
machine. If you want to check it from your own phone/laptop on the same
Wi-Fi, you'd need to change the bind address in `tracker_server.py` —
not recommended without adding authentication first, since anyone on
the network could otherwise reach it.

## Limitations

- This is a simple, transparent usage logger — not a full parental
  control suite. It doesn't block apps, filter content, or enforce
  limits by itself.
- Time is attributed to whichever window is focused; if two windows are
  open side by side, only the focused one accumulates time.
- The website list shows **visits**, not time-per-site — Firefox's
  history doesn't record how long a page stayed open, only that it was
  visited. Overall Firefox time is still covered by the app-usage
  tracking above.
- Only Firefox is covered. Chrome/Edge could be added the same way
  later (their history databases work similarly) if needed.
- Private Browsing windows are intentionally not visible — Firefox
  itself never writes those to history, by design.
- The tracker must run under the same Windows account as the browser
  (see "Running it automatically" above) to read that profile's history.
- If you want enforced time limits and content filtering, Windows has
  a built-in option: **Microsoft Family Safety**
  (account.microsoft.com/family) — this project is a good complement
  for a quick local view of usage.
