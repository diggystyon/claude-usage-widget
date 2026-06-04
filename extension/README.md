# Claude Usage Bridge (browser extension)

This is the optional companion to the **Claude Usage** tray widget. It runs in the background of your browser and forwards your live `claude.ai` usage to the widget over `127.0.0.1:38080`. Once installed, the widget never needs cookies copied or pasted — it's hands-off forever as long as you stay signed into claude.ai in this browser.

## Install in Microsoft Edge

1. Open `edge://extensions/`.
2. Toggle **Developer mode** ON (left sidebar).
3. Click **Load unpacked**.
4. Select this `extension` folder.
5. Toggle **Developer mode** back OFF.
   The extension keeps running (because "Allow extensions from other stores" is on by default), and you'll never see the "Turn off extensions in developer mode" warning popup again.

That's it. As long as you stay signed into https://claude.ai in this browser, the widget updates within a minute, automatically.

## Install in Google Chrome

Same as Edge but at `chrome://extensions/`.

## Verify it's working

1. Open the tray widget. Tooltip shows **Source: Claude browser extension** within ~60 seconds.
2. The extension icon in your toolbar should have no badge. If you see a `?` or `!`:
   - **Hover the toolbar icon.** If the tooltip says "sign in to claude.ai in this browser", that's the fix -- open https://claude.ai and sign in.
   - **Click the toolbar icon.** That opens claude.ai in a new tab for you. Once you're signed in, the badge clears within a minute and the widget catches up.
   - **Widget isn't running.** The extension doesn't badge for this case (it just silently backs off), but if the widget tray icon is gone, relaunch it from the Start Menu.

## What it does (and doesn't)

It uses one permission, `host_permissions`, scoped to:

- `https://claude.ai/*` — to fetch your usage.
- `http://127.0.0.1:38080/*` — to deliver it to the widget.

Nothing leaves your machine. The local POST goes only to localhost; the listener only accepts requests with a `chrome-extension://` or `moz-extension://` Origin so other local apps can't push fake values.

The extension does NOT:

- Read or modify any page content.
- Open tabs.
- Send anything to a remote server.
- Persist anything other than your org id (cached so it doesn't refetch every minute).

## Uninstall

`edge://extensions/` → Claude Usage Bridge → Remove.
