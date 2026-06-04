# Claude Usage Widget

A desktop widget that shows two colored bars in your system tray (Windows) or menu bar (macOS):

- **Top bar** = current session usage (the 5-hour window)
- **Bottom bar** = 7-day weekly usage

Hover (or click) for exact percentages and reset times. Bars use a smooth green - yellow - red gradient as usage rises. The icon also shows a small colored dot if [status.claude.com](https://status.claude.com) is reporting a claude.ai outage.

**macOS users:** the install + usage flow is documented separately. See [README-MAC.md](README-MAC.md).

The rest of this document is for the Windows build.

## Quick start

These four steps get most users to working bars. Each step takes under a minute.

1. **Install the widget.** Download **`Claude Usage Setup.exe`** from the [latest release](https://github.com/diggystyon/claude-usage-widget/releases/latest), double-click, click through the wizard. The widget launches into your system tray.

2. **Install the browser extension** (one-time, ~30 seconds). After the installer finishes it opens File Explorer at the extension folder and Edge at `edge://extensions/`. In Edge:
   - Toggle **Developer mode** on (left sidebar).
   - Click **Load unpacked** and pick the folder Explorer just opened (also at `%LOCALAPPDATA%\Claude Usage\extension\`).
   - Toggle **Developer mode** back off. The extension keeps running.

   Chrome works identically at `chrome://extensions/`. Firefox isn't supported yet.

3. **Sign in to claude.ai in this same browser.** Open a new tab in Edge (or Chrome), go to https://claude.ai, sign in if prompted. **This is the step most setups forget.** The extension reads cookies from the browser it's installed in -- being signed in via the Claude desktop app or a different browser does not count.

4. **Watch the tray.** Within ~60 seconds the bars catch up and the tooltip shows **Source: Claude browser extension**. Done.

> Windows SmartScreen may warn on first launch since the installer isn't code-signed. Click **More info** > **Run anyway**.

To uninstall: Settings > Apps > Installed apps > Claude Usage > Uninstall.

## Verify it's working

Hover the tray icon. The tooltip's **Source:** line tells you which path is active:

| Tooltip says                  | Meaning                                                                                      |
| ----------------------------- | -------------------------------------------------------------------------------------------- |
| `Source: browser ext. (Xs ago)` | Working. The extension is pushing fresh data every minute.                                  |
| `Source: desktop app`         | Working via auto-read. You're on the standalone Electron Claude desktop install; no extension needed. |
| `Source: manual paste`        | Working via the one-shot cURL paste fallback. Will go stale in a few hours.                  |
| `Claude Usage: setup needed`  | First run, no successful read yet. The bars show a dash, not 0%. Finish setup: right-click the tray icon > **Set up / fix browser extension**. |
| `Sign in to claude.ai in your browser` | The extension is installed but claude.ai is rejecting it. The bars dim to show they're stale. Sign in to claude.ai in the same browser. |
| `Cookies expired - sign in to Claude desktop` | No extension, and the desktop / cURL cookies have gone stale. Install the extension or sign back in. |

The icon itself signals state at a glance: bright bars = live, **dimmed** bars = known-stale (sign-in needed), a **centered dash** = no reading yet (setup needed).

If you also see a **`!`** or **`?`** badge on the Claude Usage Bridge icon in your browser toolbar, claude.ai is rejecting the extension's calls -- sign in to https://claude.ai in that browser. Clicking the toolbar icon opens claude.ai for you.

**Stuck?** Right-click the tray icon and choose **Set up / fix browser extension** any time. It opens claude.ai (so you can sign in) and the extension folder (for first-time load-unpacked).

## If you only use the standalone Electron Claude desktop

The widget can read cookies directly from the standalone Electron build of [Claude desktop](https://claude.ai/download). On that setup the extension is genuinely optional -- the tooltip will read **Source: desktop app** and you can skip steps 2 and 3 above.

Two cases where this auto-read does *not* work and the browser extension is required:

- **Microsoft Store / UWP install of Claude desktop.** The widget intentionally never touches UWP-packaged Claude -- even brief read attempts would hold handles into the package container that prevent Microsoft Store from upgrading Claude in place.
- **Chrome 127+ App-Bound Encryption.** When Claude desktop's bundled Chromium rolls to v127 or later, its cookies become unreadable from any process outside the browser. The browser extension sidesteps this by running inside the browser's own security context.

When in doubt, just install the extension -- it works in every case.

## Manual fallback (cURL paste)

If you can't or won't install the extension, right-click the tray > **Paste cURL command (fallback)...** lets you supply cookies from a browser tab. This works one-shot but cookies expire every few hours, so it's not a daily workflow.

## How the browser extension works (technical)

The extension drops at `%LOCALAPPDATA%\Claude Usage\extension\` and is also linked from the Start Menu folder. Once loaded:

- Every 60 seconds it fetches `https://claude.ai/api/organizations/<org>/usage` using the cookies in your current browser session.
- It POSTs the JSON to `http://127.0.0.1:38080/usage` so the widget can render it. The listener binds to localhost only and only accepts payloads from a `chrome-extension://` or `moz-extension://` Origin.
- If it can't reach the widget for several minutes (e.g., you closed it), it backs off automatically up to 15-minute intervals.
- The extension stores nothing remotely. Its only persistent state is your org id, cached locally so it doesn't refetch every minute.

If claude.ai rejects the extension's calls for several minutes in a row, the toolbar tooltip changes to "Claude Usage Bridge -- sign in to claude.ai in this browser" and clicking the icon opens claude.ai.

## How updates work

The widget checks GitHub Releases once a day. If a newer version is available, it pops a toast and the right-click menu's "Check for updates..." item opens the releases page so you can grab the new installer. On a transient failure (network blip, GitHub down) the next check is in 1 hour instead of waiting the full day.

## Build from source

Requires Python 3.10+ on `PATH`. One-time prerequisite for the installer step: [Inno Setup 6](https://jrsoftware.org/isdl.php) (free, install once).

1. Double-click **`build.bat`** - installs Python deps, generates `app.ico` and the extension's PNG icons, and produces `dist\Claude Usage.exe` via PyInstaller.
2. Double-click **`make_installer.bat`** - wraps the .exe + extension folder into `installer_dist\Claude Usage Setup.exe`.

To test without an installer: `python claude_usage_tray.py` from a terminal in this folder.

## Files

- `claude_usage_tray.py` - the tray app
- `claude_api.py` - shared HTTP/version/status helpers (Windows + Mac)
- `cookie_sources.py` - Claude desktop cookie discovery and decryption (standalone Electron only)
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
