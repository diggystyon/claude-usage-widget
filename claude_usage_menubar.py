"""
Claude Usage (macOS menu bar)
-----------------------------
macOS port of the Claude Usage widget. Sits in the menu bar showing two
colored bars:
  - top    = current session (5-hour) usage
  - bottom = 7-day weekly usage

Hover the icon (or click) for exact percentages and reset times. Click the
icon for the menu (Refresh now / Quit / etc.).

Requires the Claude desktop app installed and signed in -- the widget reads
its cookies from ~/Library/Application Support/Claude/ to call the
claude.ai usage API. On macOS we don't need a browser extension because
Electron's cookie store isn't locked the way it is on Windows.

Config + log live in:  ~/Library/Application Support/ClaudeUsageTray/
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import rumps
from PIL import Image, ImageDraw

import mac_cookie_sources

APP_NAME = "ClaudeUsageTray"
APP_DISPLAY_NAME = "Claude Usage"
BUNDLE_NAME = "Claude Usage.app"
APP_INSTALL_PATH = "/Applications/Claude Usage.app"
CLAUDE_APP_PATH = "/Applications/Claude.app"
CLAUDE_DOWNLOAD_URL = "https://claude.ai/download"
__version__ = "1.1.1"

# Update check uses the same GitHub Releases endpoint as the Windows build.
UPDATE_VERSION_URL  = "https://api.github.com/repos/diggystyon/claude-usage-widget/releases/latest"
UPDATE_DOWNLOAD_URL = "https://github.com/diggystyon/claude-usage-widget/releases/latest"

CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "menubar.log"
SETUP_MARKER = CONFIG_DIR / "setup_complete"
ICON_TMP = CONFIG_DIR / "icon_current.png"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

_log_level = logging.DEBUG if os.environ.get("CLAUDE_USAGE_DEBUG") else logging.INFO
_log_handler = logging.handlers.RotatingFileHandler(
    str(LOG_FILE), maxBytes=500_000, backupCount=2, encoding="utf-8",
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().setLevel(_log_level)
logging.getLogger().addHandler(_log_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


DEFAULT_CONFIG = {
    "auto_refresh": True,
    "notify_on_failure": True,
    "session_pct": 0,
    "weekly_pct": 0,
    "session_resets_at": "",
    "weekly_resets_at": "",
    "poll_seconds": 60,
    "last_source": "",
    "last_failure_notified_at": 0,
    "org_id": "",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            logging.exception("Failed to load config; using defaults")
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("Failed to save config")


# ---------- formatting helpers (identical to the Windows version) ----------

def fmt_time_12h(t: Optional[float] = None) -> str:
    lt = time.localtime(t)
    s = time.strftime("%I:%M:%S %p", lt)
    if s.startswith("0"):
        s = s[1:]
    return s


def fmt_reset(iso_str: str, short: bool = False) -> str:
    if not iso_str:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = (dt - now).total_seconds()
        if secs <= 0:
            return "any moment"
        hrs = int(secs // 3600)
        mins = int((secs % 3600) // 60)
        if short or secs < 86400:
            if hrs >= 1:
                return f"in {hrs}h {mins}m"
            return f"in {mins}m"
        local = dt.astimezone()
        hour_str = local.strftime("%I %p").lstrip("0")
        return local.strftime("%a ") + hour_str
    except Exception:
        return ""


# ---------- icon rendering ----------

def color_for_pct(pct: float) -> Tuple[int, int, int]:
    pct = max(0.0, min(100.0, float(pct)))
    if pct < 50:
        t = pct / 50.0
        r = int(60 + (235 - 60) * t); g = 200; b = 60
    else:
        t = (pct - 50) / 50.0
        r = 235; g = int(200 + (60 - 200) * t); b = 60
    return (r, g, b)


def render_icon(session_pct: float, weekly_pct: float, size: int = 44,
                status_dot: Optional[Tuple[int, int, int]] = None) -> Image.Image:
    """Render the two-bar icon. size is the @2x retina dimension; macOS
    will auto-scale this down to ~22pt on standard displays."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg_radius = max(4, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=bg_radius,
                           fill=(26, 28, 34, 235))
    margin = max(3, size // 10)
    bar_h = max(6, int(size * 0.28))
    gap = max(3, size // 14)
    bar_w = size - 2 * margin
    total_h = bar_h * 2 + gap
    top_y = (size - total_h) // 2
    bot_y = top_y + bar_h + gap
    bar_radius = max(2, bar_h // 3)
    for y, pct in ((top_y, session_pct), (bot_y, weekly_pct)):
        draw.rounded_rectangle([margin, y, margin + bar_w, y + bar_h],
                               radius=bar_radius, fill=(58, 62, 74, 255))
        clamped = max(0.0, min(100.0, float(pct)))
        fill_w = int(round(bar_w * (clamped / 100.0)))
        if fill_w >= 2:
            r, g, b = color_for_pct(clamped)
            draw.rounded_rectangle([margin, y, margin + fill_w, y + bar_h],
                                   radius=bar_radius, fill=(r, g, b, 255))
    if status_dot is not None:
        d = max(6, size // 4)
        pad = max(1, size // 32)
        x0 = size - d - pad
        y0 = pad
        draw.ellipse([x0 - 1, y0 - 1, x0 + d + 1, y0 + d + 1],
                     fill=(255, 255, 255, 230))
        draw.ellipse([x0, y0, x0 + d, y0 + d], fill=status_dot + (255,))
    return img


# ---------- claude.ai status integration ----------

STATUS_COLORS = {
    "operational":          ("All systems operational", None),
    "degraded_performance": ("Degraded performance",    (255, 200, 0)),
    "partial_outage":       ("Partial outage",          (255, 140, 0)),
    "major_outage":         ("Major outage",            (235, 60, 60)),
    "under_maintenance":    ("Under maintenance",       (80, 140, 220)),
}
CLAUDE_STATUS_URL = "https://status.claude.com/"


def _status_client() -> Optional[httpx.Client]:
    """Three-strategy SSL setup for the status endpoint. PyInstaller bundles
    can fail to find certifi's CA file from a plain ssl.create_default_context
    call; we fall back to certifi explicitly, then verify=False as a last
    resort. Public read-only endpoint, so verify=False is acceptable here."""
    try:
        return httpx.Client(timeout=10.0)
    except Exception as e:
        logging.warning("status client default SSL setup failed: %s", e)
    try:
        import certifi
        return httpx.Client(timeout=10.0, verify=certifi.where())
    except Exception as e:
        logging.warning("status client certifi SSL setup failed: %s", e)
    try:
        return httpx.Client(timeout=10.0, verify=False)
    except Exception:
        logging.exception("status client unverified setup also failed")
        return None


def fetch_claude_status() -> Optional[Tuple[str, str]]:
    client = _status_client()
    if client is None:
        return None
    try:
        with client as c:
            r = c.get("https://status.claude.com/api/v2/components.json")
            if r.status_code != 200:
                return None
            data = r.json()
        for c in data.get("components", []):
            name = (c.get("name") or "").strip().lower()
            if name == "claude.ai":
                key = c.get("status") or "operational"
                label = STATUS_COLORS.get(key,
                                          (key.replace("_", " ").title(), None))[0]
                return (key, label)
    except Exception:
        logging.exception("status fetch failed")
    return None


# ---------- version / update check ----------

def _parse_version(s: str) -> Tuple[int, ...]:
    s = (s or "").strip().lstrip("vV")
    s = s.split()[0] if s else ""
    out = []
    for piece in s.split("."):
        try:
            out.append(int("".join(ch for ch in piece if ch.isdigit())))
        except ValueError:
            out.append(0)
    return tuple(out) if out else (0,)


def fetch_latest_version() -> Optional[str]:
    if not UPDATE_VERSION_URL:
        return None
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.get(
                UPDATE_VERSION_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"claude-usage-widget/{__version__}",
                },
            )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("draft") or data.get("prerelease"):
            return None
        tag = data.get("tag_name") or ""
        return tag.lstrip("vV") or None
    except Exception:
        logging.exception("update check failed")
    return None


# ---------- macOS notification ----------

def show_notification(title: str, msg: str, subtitle: str = "") -> None:
    """Pop a macOS notification via osascript. No external deps, just
    /usr/bin/osascript which is on every Mac."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    parts = [f'display notification "{esc(msg)}"',
             f'with title "{esc(title)}"']
    if subtitle:
        parts.insert(1, f'subtitle "{esc(subtitle)}"')
    script = " ".join(parts)
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, timeout=5)
    except Exception:
        logging.exception("notification failed")


def show_dialog(text: str, buttons: List[str], default: Optional[str] = None,
                icon: str = "note") -> Optional[str]:
    """Pop a blocking macOS dialog. Returns the label of the clicked button,
    or None on error/cancel. icon: 'note' | 'caution' | 'stop'."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    btn_list = "{" + ", ".join(f'"{esc(b)}"' for b in buttons) + "}"
    parts = [f'display dialog "{esc(text)}"',
             f'buttons {btn_list}']
    if default and default in buttons:
        parts.append(f'default button "{esc(default)}"')
    parts.append(f'with icon {icon}')
    script = " ".join(parts)
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return None
        # osascript prints e.g. "button returned:Open Download Page"
        out = (r.stdout or "").strip()
        for line in out.splitlines():
            if line.startswith("button returned:"):
                return line.split(":", 1)[1]
        return None
    except Exception:
        logging.exception("dialog failed")
        return None


# ---------- claude.ai usage fetcher (same logic as Windows version) ----------

class ClaudeUsageFetcher:
    BASE = "https://claude.ai"
    USAGE_PATH = "/api/organizations/{org}/usage"
    DEFAULT_UA = mac_cookie_sources.UA_DESKTOP
    SESSION_KEYS = ("five_hour", "current_session", "session")
    WEEKLY_KEYS = ("seven_day", "weekly", "week")

    def __init__(self, cookies, user_agent="", org_id=""):
        self.cookies = dict(cookies or {})
        self.org_id = (org_id or "").strip()
        ua = user_agent or self.DEFAULT_UA
        headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://claude.ai",
            "Referer": "https://claude.ai/settings/billing",
            "sec-ch-ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        self.client = httpx.Client(
            headers=headers, cookies=self.cookies, timeout=20.0,
            follow_redirects=True,
        )

    def close(self):
        try: self.client.close()
        except Exception: pass

    def discover_org_id(self) -> Optional[str]:
        try:
            r = self.client.get(f"{self.BASE}/api/organizations")
            if r.status_code != 200:
                logging.warning("/api/organizations -> %s", r.status_code)
                return None
            data = r.json()
            if isinstance(data, list) and data:
                org_id = data[0].get("uuid") or data[0].get("id")
                if isinstance(org_id, str):
                    return org_id
        except Exception:
            logging.exception("discover_org_id error")
        return None

    def fetch(self):
        if not self.cookies:
            return None
        if not self.org_id:
            self.org_id = self.discover_org_id() or ""
            if not self.org_id:
                return None
        url = self.BASE + self.USAGE_PATH.format(org=self.org_id)
        try:
            r = self.client.get(url)
        except Exception:
            logging.exception("usage request error")
            return None
        if r.status_code != 200:
            logging.info("usage endpoint -> %s body=%s",
                         r.status_code, r.text[:300].replace("\n", " "))
            return None
        try:
            data = r.json()
        except Exception:
            return None
        return self._extract_percents(data)

    @classmethod
    def _extract_percents(cls, data):
        def to_pct(v):
            try: f = float(v)
            except (TypeError, ValueError): return None
            if 0 <= f <= 100.0:
                return f
            return None
        s = w = None
        s_reset = w_reset = ""
        if isinstance(data, dict):
            for k in cls.SESSION_KEYS:
                v = data.get(k)
                if isinstance(v, dict) and "utilization" in v:
                    p = to_pct(v["utilization"])
                    if p is not None:
                        s = p
                        r = v.get("resets_at")
                        if isinstance(r, str):
                            s_reset = r
                        break
            for k in cls.WEEKLY_KEYS:
                v = data.get(k)
                if isinstance(v, dict) and "utilization" in v:
                    p = to_pct(v["utilization"])
                    if p is not None:
                        w = p
                        r = v.get("resets_at")
                        if isinstance(r, str):
                            w_reset = r
                        break
        if s is None or w is None:
            return None
        return (s, w, s_reset, w_reset)


# ---------- macOS-specific setup ----------

def _our_app_bundle_path() -> Optional[str]:
    """Return our own .app bundle path if we're running frozen inside one.
    sys.executable in a py2app/pyinstaller .app is
    /Applications/Claude Usage.app/Contents/MacOS/Claude Usage."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        return str(exe.parent.parent.parent)
    return None


def _is_login_item_registered() -> bool:
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "System Events" to get the name of every login item'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False
        names = [n.strip() for n in (r.stdout or "").split(",")]
        return any(APP_DISPLAY_NAME in n for n in names)
    except Exception:
        return False


def _register_as_login_item() -> bool:
    """Register our .app bundle as a login item so the widget auto-starts.
    Returns True on success, False otherwise."""
    bundle = _our_app_bundle_path() or APP_INSTALL_PATH
    if not Path(bundle).is_dir():
        logging.info("login item: %s not installed yet, skipping", bundle)
        return False
    if _is_login_item_registered():
        logging.info("login item: already registered")
        return True
    script = (
        f'tell application "System Events" to make login item at end '
        f'with properties {{path:"{bundle}", hidden:true}}'
    )
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            logging.info("login item registered: %s", bundle)
            return True
        logging.warning("login item register failed: %s", r.stderr.strip())
    except Exception:
        logging.exception("login item register failed")
    return False


def _open_url(url: str) -> None:
    try:
        subprocess.Popen(["/usr/bin/open", url])
    except Exception:
        logging.exception("open url failed: %s", url)


def _ensure_claude_app_or_prompt() -> bool:
    """Block until Claude.app is installed, or user gives up.
    Returns True if Claude.app is present, False if user cancelled."""
    while True:
        if Path(CLAUDE_APP_PATH).is_dir():
            return True
        choice = show_dialog(
            "Claude Usage needs the Claude desktop app installed and signed in.\n\n"
            "Click 'Open Download Page' to install it, then come back and click "
            "'I've Installed It'.",
            buttons=["Quit", "Open Download Page", "I've Installed It"],
            default="Open Download Page",
            icon="caution",
        )
        if choice == "Open Download Page":
            _open_url(CLAUDE_DOWNLOAD_URL)
            time.sleep(2)
            continue
        if choice == "I've Installed It":
            continue
        return False


def _first_launch_setup() -> bool:
    """Run once: verify Claude.app, register login item, write marker.
    Returns True on success, False if user quit before completion."""
    if SETUP_MARKER.exists():
        return True
    if not _ensure_claude_app_or_prompt():
        return False
    _register_as_login_item()
    show_dialog(
        f"You're all set. {APP_DISPLAY_NAME} will live in your menu bar "
        "(top-right of your screen). It updates every minute.",
        buttons=["OK"], default="OK", icon="note",
    )
    try:
        SETUP_MARKER.write_text("done")
    except Exception:
        pass
    return True


# ---------- menu bar app ----------

class MenuBarApp(rumps.App):
    EXTENSION_FRESHNESS_SEC = 180

    def __init__(self):
        self._write_icon(0, 0, None)
        super().__init__(
            APP_DISPLAY_NAME,
            icon=str(ICON_TMP),
            title="",
            quit_button=None,
            template=False,
        )

        self.config = load_config()
        self.session_pct: float = float(self.config.get("session_pct", 0))
        self.weekly_pct: float = float(self.config.get("weekly_pct", 0))
        self.claude_status_key: str = "operational"
        self.claude_status_label: str = "All systems operational"
        self.latest_version: Optional[str] = None
        self.stop_evt = threading.Event()
        self.last_fetch_at: float = 0.0

        self.mi_pct       = rumps.MenuItem("Session: --%   Weekly: --%")
        self.mi_source    = rumps.MenuItem("Source: -")
        self.mi_status    = rumps.MenuItem("Claude.ai: -")
        self.mi_refresh   = rumps.MenuItem("Refresh now",
                                           callback=self.cb_refresh)
        self.mi_status_pg = rumps.MenuItem("Open Claude status page",
                                           callback=self.cb_status_page)
        self.mi_version   = rumps.MenuItem(self._version_label(),
                                           callback=self.cb_version)
        self.mi_auto      = rumps.MenuItem("Auto-refresh from Claude app",
                                           callback=self.cb_toggle_auto)
        self.mi_notify    = rumps.MenuItem("Notify on failure",
                                           callback=self.cb_toggle_notify)
        self.mi_log       = rumps.MenuItem("Open log folder",
                                           callback=self.cb_open_log)
        self.mi_quit      = rumps.MenuItem("Quit Claude Usage",
                                           callback=self.cb_quit)

        self.mi_auto.state   = 1 if self.config.get("auto_refresh", True) else 0
        self.mi_notify.state = 1 if self.config.get("notify_on_failure", True) else 0

        for itm in (self.mi_pct, self.mi_source, self.mi_status):
            itm.set_callback(None)

        self.menu = [
            self.mi_pct,
            self.mi_source,
            self.mi_status,
            None,
            self.mi_refresh,
            self.mi_status_pg,
            self.mi_version,
            self.mi_auto,
            self.mi_notify,
            None,
            self.mi_log,
            self.mi_quit,
        ]

        threading.Thread(target=self._poll_loop,         daemon=True).start()
        threading.Thread(target=self._status_poll_loop,  daemon=True).start()
        threading.Thread(target=self._update_check_loop, daemon=True).start()

    # ----- icon writing -----

    def _write_icon(self, session_pct: float, weekly_pct: float,
                    dot: Optional[Tuple[int, int, int]]) -> None:
        img = render_icon(session_pct, weekly_pct, size=44, status_dot=dot)
        try:
            img.save(str(ICON_TMP), format="PNG")
        except Exception:
            logging.exception("icon save failed")

    def _refresh_icon_and_menu(self) -> None:
        dot = STATUS_COLORS.get(self.claude_status_key, (None, None))[1]
        self._write_icon(self.session_pct, self.weekly_pct, dot)
        try:
            self.icon = str(ICON_TMP)
        except Exception:
            logging.exception("icon reassign failed")

        src = self.config.get("last_source") or "-"
        self.mi_pct.title = (
            f"Session: {self.session_pct:.0f}%   "
            f"Weekly: {self.weekly_pct:.0f}%"
        )
        s_reset = fmt_reset(self.config.get("session_resets_at", ""), short=True)
        w_reset = fmt_reset(self.config.get("weekly_resets_at", ""), short=False)
        if s_reset and w_reset:
            self.mi_pct.title += f"   |   resets {s_reset} / {w_reset}"
        self.mi_source.title = f"Source: {src}"
        self.mi_status.title = f"Claude.ai: {self.claude_status_label}"
        self.mi_version.title = self._version_label()

    # ----- version label -----

    def _version_label(self) -> str:
        if self.latest_version and \
                _parse_version(self.latest_version) > _parse_version(__version__):
            return f"Update available: v{self.latest_version}  (click to download)"
        return f"{APP_DISPLAY_NAME} v{__version__}  (click for releases)"

    # ----- menu callbacks -----

    def cb_refresh(self, _) -> None:
        threading.Thread(target=self._fetch_once, daemon=True).start()
        threading.Thread(target=self._check_status_once, daemon=True).start()

    def _check_status_once(self) -> None:
        """One-shot status fetch + UI refresh. Lets the user force a status
        update via 'Refresh now' without waiting for the 5-minute poll."""
        try:
            res = fetch_claude_status()
            if res is not None:
                self.claude_status_key, self.claude_status_label = res
                self._refresh_icon_and_menu()
        except Exception:
            logging.exception("manual status check failed")

    def cb_status_page(self, _) -> None:
        _open_url(CLAUDE_STATUS_URL)

    def cb_version(self, _) -> None:
        _open_url(UPDATE_DOWNLOAD_URL)

    def cb_toggle_auto(self, item) -> None:
        item.state = 0 if item.state else 1
        self.config["auto_refresh"] = bool(item.state)
        save_config(self.config)

    def cb_toggle_notify(self, item) -> None:
        item.state = 0 if item.state else 1
        self.config["notify_on_failure"] = bool(item.state)
        save_config(self.config)

    def cb_open_log(self, _) -> None:
        try:
            subprocess.Popen(["/usr/bin/open", str(CONFIG_DIR)])
        except Exception:
            logging.exception("open log folder failed")

    def cb_quit(self, _) -> None:
        self.stop_evt.set()
        rumps.quit_application()

    # ----- background loops -----

    def _build_fetcher(self) -> Tuple[Optional[ClaudeUsageFetcher], str]:
        if not self.config.get("auto_refresh", True):
            return (None, "auto-refresh off")
        res = mac_cookie_sources.fetch_cookies()
        if res is None:
            return (None, "Claude desktop app not signed in or keychain blocked")
        source_label, cookies, ua = res
        f = ClaudeUsageFetcher(cookies, user_agent=ua,
                               org_id=self.config.get("org_id", ""))
        return (f, source_label)

    def _maybe_notify_failure(self, reason: str) -> None:
        if not self.config.get("notify_on_failure", True):
            return
        last = float(self.config.get("last_failure_notified_at", 0))
        if time.time() - last < 3600:
            return
        self.config["last_failure_notified_at"] = time.time()
        save_config(self.config)
        show_notification(
            f"{APP_DISPLAY_NAME} couldn't refresh",
            reason,
        )

    def _fetch_once(self) -> None:
        f, source = self._build_fetcher()
        if f is None:
            self.config["last_source"] = source
            save_config(self.config)
            self._refresh_icon_and_menu()
            self._maybe_notify_failure(source)
            return
        try:
            res = f.fetch()
        finally:
            f.close()
        if res is None:
            self._maybe_notify_failure("API call failed -- see log")
            return
        s, w, s_reset, w_reset = res
        self.session_pct = s
        self.weekly_pct  = w
        self.config["session_pct"] = s
        self.config["weekly_pct"]  = w
        self.config["session_resets_at"] = s_reset
        self.config["weekly_resets_at"]  = w_reset
        self.config["last_source"] = source
        if f.org_id:
            self.config["org_id"] = f.org_id
        save_config(self.config)
        self.last_fetch_at = time.time()
        self._refresh_icon_and_menu()

    def _poll_loop(self) -> None:
        self.stop_evt.wait(2)
        while not self.stop_evt.is_set():
            try:
                self._fetch_once()
            except Exception:
                logging.exception("fetch loop error")
            self.stop_evt.wait(int(self.config.get("poll_seconds", 60)))

    def _status_poll_loop(self) -> None:
        while not self.stop_evt.is_set():
            try:
                res = fetch_claude_status()
                if res:
                    key, label = res
                    self.claude_status_key = key
                    self.claude_status_label = label
                    self._refresh_icon_and_menu()
            except Exception:
                logging.exception("status loop error")
            self.stop_evt.wait(300)

    def _update_check_loop(self) -> None:
        notified_for: Optional[str] = None
        while not self.stop_evt.is_set():
            try:
                latest = fetch_latest_version()
                if latest:
                    self.latest_version = latest
                    if (_parse_version(latest) > _parse_version(__version__)
                            and notified_for != latest):
                        show_notification(
                            f"{APP_DISPLAY_NAME} update available",
                            f"You're on v{__version__}. Click the menu bar icon "
                            f"to download v{latest}.",
                        )
                        notified_for = latest
                    self._refresh_icon_and_menu()
            except Exception:
                logging.exception("update check loop error")
            self.stop_evt.wait(24 * 3600)


# ---------- entry point ----------

def main() -> int:
    logging.info("%s starting (v%s) on %s",
                 APP_DISPLAY_NAME, __version__, sys.platform)
    if sys.platform != "darwin":
        print("This build only runs on macOS.", file=sys.stderr)
        return 1
    try:
        if not _first_launch_setup():
            logging.info("user cancelled first-launch setup; exiting")
            return 0
    except Exception:
        logging.exception("first-launch setup error")
    try:
        MenuBarApp().run()
    except Exception:
        logging.exception("fatal error in MenuBarApp")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
