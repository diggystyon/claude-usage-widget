# Claude Usage (macOS menu bar)

Shows your current Claude.ai session and weekly usage as two colored bars in your menu bar (top-right of the screen).

## What you need first

1. **The Claude desktop app**, installed and signed in. Get it from [claude.ai/download](https://claude.ai/download). The widget reads its sign-in cookies so it can fetch your usage from Claude. Without it, the widget has nothing to read.
2. **macOS 12 (Monterey) or newer.** Should work on Apple Silicon and Intel Macs.

## Install

1. Download `Claude Usage.dmg` from the [latest release](https://github.com/diggystyon/claude-usage-widget/releases/latest).
2. Double-click the `.dmg` file. A window opens showing the app icon and an arrow pointing at the Applications folder.
3. **Drag the app icon onto the Applications folder shortcut.**
4. Open the Applications folder, find **Claude Usage**.
5. **Right-click on Claude Usage and choose "Open"** (not double-click — the right-click is important the first time).
6. macOS shows a dialog: *"Apple cannot check it for malicious software."* Click **Open**.

You'll see a small icon appear in your menu bar with two colored bars: top is your 5-hour session usage, bottom is your weekly usage. Click the icon for the menu.

## Why the right-click on first launch?

Because this app isn't signed by Apple's $99/yr developer program. Right-click → Open is the macOS way of saying "I trust this app from a non-App-Store source." You only do this once — after the first launch, double-clicking works normally.

If you see "the application can't be opened" with no "Open" button on a recent macOS, try this instead:
1. System Settings → Privacy & Security
2. Scroll down to the Security section. You'll see "Claude Usage was blocked..."
3. Click **Open Anyway**.
4. Enter your password or use Touch ID when prompted.

## What the widget does

The widget polls the claude.ai usage API once a minute and updates the two bars. Bars are green at low usage, yellow in the middle, red as you approach the limit. The icon also gets a small colored dot if claude.ai itself is having an outage.

Click the menu bar icon for a popup that shows exact percentages, when each window resets, and a few actions:
- **Refresh now** — force an immediate refresh
- **Open Claude status page** — opens [status.claude.com](https://status.claude.com) in your browser
- **Auto-refresh from Claude app** — toggle on/off
- **Notify on failure** — toggle whether you get notified if a refresh fails
- **Open log folder** — opens the log file for troubleshooting
- **Quit Claude Usage** — exit

## Auto-launch at login

The widget registers itself as a login item on first launch, so it auto-starts whenever you sign in to your Mac. To turn this off, open System Settings → General → Login Items and remove Claude Usage from the list.

## Privacy

The widget reads cookies from your local Claude desktop app installation (`~/Library/Application Support/Claude/`). The first read may pop a macOS Keychain dialog ("security wants to access key 'Claude Safe Storage'") — click **Always Allow** so future reads happen silently. Nothing is sent to any third-party server. The only outbound network traffic is to `claude.ai`, `status.claude.com`, and `api.github.com` (for update checks).

## Troubleshooting

**Widget shows 0% / "Source: not signed in":**
- Open the Claude desktop app and sign in.
- Click the widget's menu bar icon → "Refresh now."

**Keychain keeps prompting:**
- When the dialog appears, click **Always Allow** (not just Allow). You should never see it again.

**Bars never update past the cached values:**
- Check the log: menu → "Open log folder." The log file `menubar.log` records what's failing.

## Updating

When a new version ships, the menu's version row flips to "Update available: vX.Y.Z (click to download)". Click it to open the releases page, download the new `.dmg`, and drag the new app over the old one in Applications.

## Uninstall

1. Delete `Claude Usage` from Applications.
2. System Settings → General → Login Items → remove Claude Usage.
3. Delete `~/Library/Application Support/ClaudeUsageTray/` if you also want the config gone.
4. Open Keychain Access, search for "Claude Safe Storage", and remove the access rule for Claude Usage if you want to revoke its keychain permission.
