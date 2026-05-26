# Claude Usage Widget

A desktop widget that shows two colored bars in your system tray (Windows) or menu bar (macOS):

- **Top bar** = current session usage (the 5-hour window)
- **Bottom bar** = 7-day weekly usage

Hover (or click) for exact percentages and reset times. Bars use a smooth green - yellow - red gradient as usage rises. The icon also shows a small colored dot if [status.claude.com](https://status.claude.com) is reporting a claude.ai outage.

**macOS users:** the install + usage flow is documented separately. See [README-MAC.md](README-MAC.md).

The rest of this document is for the Windows build.

## Quick start (recommended)

1. Download **`Claude Usage Setup.exe`** from the [latest release](https://github.com/diggystyon/claude-usage-widget/releases/latest).
2. Double-click it. Click Next, leave "Start Claude Usage when Windows starts" checked, click Install.
3. The widget launches into your system tray.
4. (Optional) **Install the bundled browser extension** for hands-off auto-refresh on Chrome 127+ or on the Microsoft Store version of Claude desktop. See "Hands-off mode" below.

> Windows SmartScreen may warn on first launch since the installer isn't code-signed. Click **More info**, then **Run anyway**.

To uninstall: Settings > Apps > Installed apps > Claude Usage > Uninstall.

## What the widget needs to read your usage

The widget needs your claude.ai sign-in cookies so it can call the usage API on your behalf. It tries three sources, in priority order:

1. **Browser extension** posting to localhost (best - passive, never expires).
2. **Auto-read from the Claude desktop app's cookie store** (works for the standalone Electron install of [Claude desktop](https://claude.ai/download)). Handled by `cookie_sources.py`.
3. **Stored cookies from a manual cURL paste** (works until cookies rotate, every few hours).

Source 2 is automatic and silent on a standard install. The two cases where it falls back to the extension or cURL paste:

- **Microsoft Store / UWP install of Claude desktop**: the UWP sandbox holds an exclusive lock on its cookie database. We can't read it from outside.
- **Chrome 127+ App-Bound Encryption (v20 prefix)**: when Claude desktop's bundled Chromium rolls to v127 or later, its cookies become unreadable from any process outside the browser. The browser extension sidesteps this by running inside the browser's own security context.

If neither source 2 nor the extension works for you, the manual cURL paste is the last-resort fallback.

## Hands-off mode (browser extension)

The installer drops a small browser extension at `%LOCALAPPDATA%\Claude Usage\extension\` (also linked from the Start Menu folder).

To install it:

1. Open `edge://extensions/` (or `chrome://extensions/`).
2. Toggle **Developer mode** on.
3. Click **Load unpacked** and pick that `extension` folder.

As long as you stay signed into https://claude.ai in this browser, the extension fetches your usage every minute in the background and posts it to the widget over `127.0.0.1:38080`. **No tab needs to be open**, and you never paste cookies again. The browser process itself just needs to be running, which on Windows it usually is anyway.

The widget tooltip will show **Source: Claude browser extension** within a minute, confirming the bridge is working. The localhost listener only accepts requests with a `chrome-extension://` or `moz-extension://` Origin and rejects payloads larger than 64 KB.

If the extension can't reach the widget for several minutes in a row, it backs off automatically (up to 15-minute intervals) so it doesn't waste battery polling localhost when the widget isn't running.

## Manual fallback (cURL paste)

If the auto-read from the Claude desktop app doesn't work and you don't want the extension, right-click the tray > **Paste cURL command (fallback)...** lets you supply cookies from a browser tab. This works one-shot but cookies expire every few hours, so it's not a daily workflow.

## How updates work

The widget checks GitHub Releases once a day. If a newer version is available, it pops a toast and the right-click menu's "Check for updates..." item opens the releases page so you can grab the new installer. On a transient failure (network blip, GitHub down) the next check is in 1 hour instead of waiting the full day.

## Build from source

Requires Python 3.10+ on `PATH`. One-time prerequisite for the installer step: [Inno Setup 6](https://jrsoftware.org/isdl.php) (free, install once).

1. Double-click **`build.bat`** - installs Python deps, generates `app.ico` and the extension's PNG icons, and produces `dist\Claude Usage.exe` via PyInstaller.
2. Double-click **`make_installer.bat`** - wraps the .exe + extension folder into `installer_dist\Claude Usage Setup.exe`.

To test without an installer: `python claude_usage_tray.py` from a terminal in this folder.

## Files

- `claude_usage_tray.py` - the tray app
- `cookie_sources.py` - Claude desktop cookie discovery and decryption (primary source on standard Electron installs)
- `extension/` - browser extension (Manifest V3 service worker)
- `make_icon.py` - generates `app.ico` and the extension's PNG icons
- `requirements.txt` - Python deps (installed by `build.bat`)
- `build.bat` - install deps + build the .exe
- `installer.iss` - Inno Setup script for the installer
- `make_installer.bat` - produces `Claude Usage Setup.exe`
- `dist\Claude Usage.exe` - the bundled widget (created by `build.bat`)
- `installer_dist\Claude Usage Setup.exe` - the friendly installer (created by `make_installer.bat`)

Config and logs live in `%APPDATA%\ClaudeUsageTray\`:

- `config.json` - your settings
- `tray.log` - capped at ~1.5 MB (rotation: 500 KB x 3). Set environment variable `CLAUDE_USAGE_DEBUG=1` for verbose per-fetch logs.

## Caveats

- Unofficial. Uses an undocumented Anthropic endpoint. If the response shape changes, the bars stop updating until the script is patched.
- The localhost listener binds to 127.0.0.1 only; nothing is exposed to the network.
- Tray icons render at 16-32px on Windows; the bars are deliberately chunky so they're readable.
- If the icon doesn't appear, check `%APPDATA%\ClaudeUsageTray\tray.log` for errors.
