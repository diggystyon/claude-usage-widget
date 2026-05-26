# Claude Usage (macOS menu bar)

Shows your current Claude.ai session and weekly usage as two colored bars in your menu bar (top-right of the screen).

## What you need first

1. **The Claude desktop app**, installed and signed in. Get it from [claude.ai/download](https://claude.ai/download). The widget reads its sign-in cookies so it can fetch your usage from Claude. Without it, the widget has nothing to read.
2. **macOS 12 (Monterey) or newer.**
3. **Apple Silicon (M1/M2/M3/M4) or Intel Mac.** Each release ships two DMGs; download the one that matches your Mac (see below).

## Install

1. Go to the [latest release](https://github.com/diggystyon/claude-usage-widget/releases/latest). Download the right DMG for your Mac:
   - **Apple Silicon (M1/M2/M3/M4):** `Claude Usage-arm64.dmg`
   - **Intel:** `Claude Usage-x86_64.dmg`

   Not sure which you have? Apple menu > About This Mac. If "Chip" says M1/M2/M3/M4, pick arm64. If "Processor" says Intel, pick x86_64.

2. Double-click the `.dmg` file. A window opens showing the app icon and an arrow pointing at the Applications folder.
3. **Drag the app icon onto the Applications folder shortcut.**
4. Open the Applications folder and find **Claude Usage**.

### First launch (the unsigned-app dance)

The app isn't signed by Apple's $99/yr developer program, so macOS blocks it by default the first time. This is a one-time hassle; after the first successful launch, double-click works normally.

**On macOS 15 (Sequoia) or newer:**

1. Double-click `Claude Usage` in Applications. You'll see a dialog saying it can't be opened because Apple cannot verify it's free of malware.
2. Click **Done**.
3. Open **System Settings** > **Privacy & Security**.
4. Scroll down to the Security section. You'll see: *"Claude Usage was blocked to protect your Mac."*
5. Click **Open Anyway** and confirm with your password or Touch ID.

**On macOS 12-14:**

1. **Right-click** Claude Usage in Applications, choose **Open**. (Not double-click - the right-click is what matters.)
2. macOS shows a dialog: *"Apple cannot check it for malicious software."* Click **Open**.

Either way, after the first successful launch a small icon appears in your menu bar with two colored bars: top is your 5-hour session usage, bottom is your weekly usage. Click the icon for the menu.

## What the widget does

Polls the claude.ai usage API once a minute and updates the two bars. Bars are green at low usage, yellow in the middle, red as you approach the limit. The icon also gets a small colored dot if claude.ai itself is having an outage.

Click the menu bar icon for a popup that shows exact percentages, when each window resets, and a few actions:

- **Refresh now** - force an immediate refresh.
- **Open Claude status page** - opens [status.claude.com](https://status.claude.com) in your browser.
- **Auto-refresh from Claude app** - toggle on/off.
- **Notify on failure** - toggle whether you get notified if a refresh fails.
- **Open log folder** - opens the log file for troubleshooting.
- **Quit Claude Usage** - exit.

## Auto-launch at login

The widget registers itself as a login item on first launch, so it auto-starts whenever you sign in to your Mac. To turn this off, open System Settings > General > Login Items and remove Claude Usage from the list.

## Privacy

The widget reads cookies from your local Claude desktop app installation (`~/Library/Application Support/Claude/`). The first read pops a macOS Keychain dialog ("security wants to access key 'Claude Safe Storage'") - click **Always Allow** so future reads happen silently. Nothing is sent to any third-party server. The only outbound network traffic is to `claude.ai`, `status.claude.com`, and `api.github.com` (for update checks).

## Troubleshooting

**Widget shows 0% / "Source: not signed in":**

- Open the Claude desktop app and sign in.
- Click the widget's menu bar icon, then "Refresh now."

**Keychain keeps prompting on every refresh:**

- When the dialog appears, click **Always Allow** (not just Allow). You should never see it again.

**Keychain was denied (clicked "Deny" by mistake) and now the widget can't read cookies:**

The Keychain remembers your "Deny" choice and won't re-prompt automatically. To fix it:

1. Open **Keychain Access** (Spotlight: type "Keychain Access").
2. In the search bar, type **Claude Safe Storage**.
3. Double-click the matching entry. A dialog opens.
4. Go to the **Access Control** tab.
5. Click the **+** button under "Always allow access by these applications," navigate to your widget binary, and add it. Or click the radio button **Allow all applications to access this item** (less secure but simpler).
6. Click **Save Changes** and authenticate.
7. Click the widget's menu bar icon, then "Refresh now."

If you can't find the entry, the Claude desktop app may not have generated it yet. Open Claude, sign in, then try again.

**Bars never update past the cached values:**

- Check the log: menu > "Open log folder." The log file `menubar.log` records what's failing.

## Updating

When a new version ships, the menu's version row flips to "Update available: vX.Y.Z (click to download)". Click it to open the releases page, download the new `.dmg`, and drag the new app over the old one in Applications.

## Uninstall

1. Delete `Claude Usage` from Applications.
2. System Settings > General > Login Items > remove Claude Usage.
3. Delete `~/Library/Application Support/ClaudeUsageTray/` if you also want the config gone.
4. Open Keychain Access, search for "Claude Safe Storage", and remove the access rule for Claude Usage if you want to revoke its keychain permission.
