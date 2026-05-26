"""
Claude Usage Tray
-----------------
A Windows system tray widget that shows two colored bars:
  - top bar  = current session (5-hour) usage
  - bottom bar = 7-day weekly usage

Hover the icon for exact percentages. Right-click for menu actions.

Requires the Claude desktop app (https://claude.ai/download) so the widget
can read your sign-in cookies. A cURL paste fallback is available if the
desktop app isn't installed, but it requires manual refresh every few hours.

Config + log live in:  %APPDATA%\\ClaudeUsageTray\\
"""
from __future__ import annotations

import http.server
import json
import logging
import logging.handlers
import os
import shlex
import socketserver
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import pystray
from PIL import Image, ImageDraw

import cookie_sources
from _version import __version__  # single source of truth -- bump _version.py only

APP_NAME = "ClaudeUsageTray"
APP_DISPLAY_NAME = "Claude Usage"
APP_USER_MODEL_ID = "diggystyon.ClaudeUsageTray"

# --- update-check config (GitHub Releases) ---
# UPDATE_VERSION_URL hits GitHub's REST API and returns JSON for the latest
# release; we read tag_name (e.g. "v1.1.0") and strip the leading 'v'.
# UPDATE_DOWNLOAD_URL is the human-facing release page where the user can
# grab the .exe asset. Both endpoints are anonymous-accessible for public
# repos and need no auth or rate-limit headers for our 1-call-per-day usage.
UPDATE_VERSION_URL  = "https://api.github.com/repos/diggystyon/claude-usage-widget/releases/latest"
UPDATE_DOWNLOAD_URL = "https://github.com/diggystyon/claude-usage-widget/releases/latest"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "tray.log"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Rotating file handler: caps total log size at ~1.5 MB (500 KB x 3 files).
# Set CLAUDE_USAGE_DEBUG=1 in your environment to capture per-fetch DEBUG lines.
_log_level = logging.DEBUG if os.environ.get("CLAUDE_USAGE_DEBUG") else logging.INFO
_log_handler = logging.handlers.RotatingFileHandler(
    str(LOG_FILE), maxBytes=500_000, backupCount=2, encoding="utf-8",
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().setLevel(_log_level)
logging.getLogger().addHandler(_log_handler)
# Silence httpx's per-request INFO line; keep its warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _set_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        logging.exception("could not set AppUserModelID")


def _setup_bundled_certifi() -> None:
    """Make TLS verification work inside the PyInstaller bundle.

    PyInstaller's --collect-data certifi flag copies cacert.pem into the bundle,
    but at runtime certifi.where() can still return a path that doesn't resolve
    (depending on how the runtime hook fires). When that happens, httpx defaults
    fail with [Errno 2] and we silently slide to verify=False. To prevent that,
    we explicitly point SSL_CERT_FILE / REQUESTS_CA_BUNDLE at a real on-disk
    cacert.pem before any httpx.Client is constructed.

    Search order:
      1. respect an explicit SSL_CERT_FILE the user already set
      2. certifi.where() if the file exists
      3. {sys._MEIPASS}/certifi/cacert.pem (PyInstaller --onefile extraction dir)
      4. {bundle dir}/certifi/cacert.pem (PyInstaller --onedir, or Mac .app)
    """
    try:
        if os.environ.get("SSL_CERT_FILE") and os.path.isfile(os.environ["SSL_CERT_FILE"]):
            return
        candidates: List[str] = []
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "certifi", "cacert.pem"))
        # PyInstaller --onedir: data sits next to the executable.
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "certifi", "cacert.pem"))
        for path in candidates:
            if path and os.path.isfile(path):
                os.environ["SSL_CERT_FILE"] = path
                os.environ["REQUESTS_CA_BUNDLE"] = path
                logging.info("TLS CA bundle: %s", path)
                return
        logging.warning("no CA bundle found; httpx will fall back to verify=False")
    except Exception:
        logging.exception("certifi bundle setup failed")


_setup_bundled_certifi()


DEFAULT_CONFIG = {
    "mode": "auto",          # "auto" | "scrape" (manual cURL paste fallback)
    "auto_refresh": True,    # try cookie_sources on each poll
    "notify_on_failure": True,
    "session_cookie": "",
    "cookies": {},           # last manually-pasted cookies
    "user_agent": "",
    "extra_headers": {},
    "org_id": "",
    "session_pct": 0,
    "weekly_pct": 0,
    "session_resets_at": "", # ISO 8601 string from API (e.g. five_hour.resets_at)
    "weekly_resets_at": "",  # ISO 8601 string from API (seven_day.resets_at)
    "poll_seconds": 60,
    "last_source": "",
    "last_failure_notified_at": 0,
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


def fmt_time_12h(t: Optional[float] = None) -> str:
    """Return time as '5:48:30 PM' (no leading zero on hour)."""
    lt = time.localtime(t)
    s = time.strftime("%I:%M:%S %p", lt)
    # %I is 01-12; strip a leading zero if any
    if s.startswith("0"):
        s = s[1:]
    return s


def fmt_reset(iso_str: str, short: bool = False) -> str:
    """Format a 'resets_at' ISO 8601 timestamp for the tooltip.

    short=True (5-hour session window): always relative -> "in 2h 6m"
    short=False (7-day weekly window):  absolute when >24h away -> "Sat 11 PM",
                                        relative inside 24h     -> "in 18h 4m"
    """
    if not iso_str:
        return ""
    try:
        from datetime import datetime, timezone
        # Python's fromisoformat handles "2026-05-06T02:00:00.106507+00:00"
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
        # Long form: "Sat 11 PM" in local time
        local = dt.astimezone()
        hour_str = local.strftime("%I %p").lstrip("0")
        return local.strftime("%a ") + hour_str
    except Exception:
        return ""


# ---------- cURL parser (for the legacy paste flow) ----------

def parse_curl_command(curl: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    if not curl or not curl.strip():
        return {}, {}
    s = curl.strip()
    s = s.replace("\\\r\n", " ").replace("\\\n", " ")
    s = s.replace("^\r\n", " ").replace("^\n", " ")
    s = s.replace("`\r\n", " ").replace("`\n", " ")
    try:
        tokens = shlex.split(s, posix=True)
    except ValueError:
        tokens = shlex.split(s, posix=False)

    headers: Dict[str, str] = {}
    cookies: Dict[str, str] = {}

    def add_cookie_string(val: str) -> None:
        for piece in val.split(";"):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            ck, cv = piece.split("=", 1)
            cookies[ck.strip()] = cv.strip()

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            if i < len(tokens):
                kv = tokens[i]
                if ":" in kv:
                    name, val = kv.split(":", 1)
                    name = name.strip()
                    val = val.strip()
                    if name.lower() == "cookie":
                        add_cookie_string(val)
                    elif name and not name.lower().startswith(":"):
                        headers[name] = val
        elif t in ("-b", "--cookie"):
            i += 1
            if i < len(tokens):
                add_cookie_string(tokens[i])
        i += 1

    return headers, cookies


# ---------- icon rendering ----------

def color_for_pct(pct: float) -> Tuple[int, int, int]:
    """Smooth green -> yellow -> red gradient based on usage %."""
    pct = max(0.0, min(100.0, float(pct)))
    if pct < 50:
        t = pct / 50.0
        r = int(60 + (235 - 60) * t); g = 200; b = 60
    else:
        t = (pct - 50) / 50.0
        r = 235; g = int(200 + (60 - 200) * t); b = 60
    return (r, g, b)


def render_icon(session_pct: float, weekly_pct: float, size: int = 64,
                status_dot: Optional[Tuple[int, int, int]] = None) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg_radius = max(6, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=bg_radius, fill=(26, 28, 34, 235))
    margin = max(4, size // 10)
    bar_h = max(10, int(size * 0.28))
    gap = max(4, size // 14)
    bar_w = size - 2 * margin
    total_h = bar_h * 2 + gap
    top_y = (size - total_h) // 2
    bot_y = top_y + bar_h + gap
    bar_radius = bar_h // 3
    for y, pct in ((top_y, session_pct), (bot_y, weekly_pct)):
        draw.rounded_rectangle([margin, y, margin + bar_w, y + bar_h], radius=bar_radius, fill=(58, 62, 74, 255))
        clamped = max(0.0, min(100.0, float(pct)))
        fill_w = int(round(bar_w * (clamped / 100.0)))
        if fill_w >= 2:
            r, g, b = color_for_pct(clamped)
            draw.rounded_rectangle([margin, y, margin + fill_w, y + bar_h], radius=bar_radius, fill=(r, g, b, 255))
    # Status dot in the upper-right corner: indicates a claude.ai outage.
    # Hidden entirely when status is operational (dot=None).
    if status_dot is not None:
        d = max(8, size // 4)
        pad = max(1, size // 32)
        x0 = size - d - pad
        y0 = pad
        # White outline so the dot is visible on any taskbar color.
        draw.ellipse([x0 - 1, y0 - 1, x0 + d + 1, y0 + d + 1],
                     fill=(255, 255, 255, 230))
        draw.ellipse([x0, y0, x0 + d, y0 + d], fill=status_dot + (255,))
    return img


# ---------- Claude status page integration ----------

# Maps Statuspage component statuses to (display label, dot RGB).
# operational has no dot (dot suppressed when icon overlay color is None).
STATUS_COLORS = {
    "operational":          ("All systems operational", None),
    "degraded_performance": ("Degraded performance",    (255, 200, 0)),    # yellow
    "partial_outage":       ("Partial outage",          (255, 140, 0)),    # orange
    "major_outage":         ("Major outage",            (235, 60, 60)),    # red
    "under_maintenance":    ("Under maintenance",       (80, 140, 220)),   # blue
}
CLAUDE_STATUS_URL = "https://status.claude.com/"


def _status_ssl_strategies() -> List[Tuple[str, object]]:
    """Return a list of (name, verify-arg) tuples to try in order when
    talking to status.claude.com. PyInstaller's bundled ssl/certifi setup
    can fail with FileNotFoundError at first request, so we try the
    default, then certifi explicitly, then unverified as a last resort.

    The status endpoint serves public read-only data so verify=False is
    acceptable there. We do NOT use this for claude.ai cookie traffic.

    Note: previously this function returned an httpx.Client and the
    caller cascaded on client construction failures, but the constructor
    never raises -- SSL errors surface only at first request -- so the
    fallbacks were unreachable. The cascade is now at the request site.
    """
    strategies: List[Tuple[str, object]] = [("default", None)]
    try:
        import certifi
        cert_path = certifi.where()
        if os.path.isfile(cert_path):
            strategies.append(("certifi", cert_path))
    except Exception:
        pass
    strategies.append(("unverified", False))
    return strategies


def fetch_claude_status() -> Optional[Tuple[str, str]]:
    """Fetch the current 'claude.ai' component status from status.claude.com.

    Returns (status_key, label) like ('operational', 'All systems operational')
    or None on any failure (network, parse, missing component).
    Filters to ONLY the claude.ai component, ignoring the API / Console / etc.

    Tries the SSL strategies in _status_ssl_strategies() in order, falling
    through on SSL or transport failures so a missing CA bundle doesn't
    leave the status dot stuck on its last known value forever.
    """
    url = "https://status.claude.com/api/v2/components.json"
    data = None
    last_err: Optional[Exception] = None
    for name, verify in _status_ssl_strategies():
        try:
            kwargs: Dict[str, object] = {"timeout": 10.0}
            if verify is not None:
                kwargs["verify"] = verify
            with httpx.Client(**kwargs) as c:  # type: ignore[arg-type]
                r = c.get(url)
                if r.status_code != 200:
                    return None
                data = r.json()
            if name != "default":
                logging.warning("status fetch succeeded via SSL fallback %r", name)
            break
        except (httpx.TransportError, FileNotFoundError, OSError, ImportError) as e:
            last_err = e
            logging.debug("status fetch via %s failed: %s", name, e)
            continue
        except Exception:
            logging.exception("status fetch via %s raised", name)
            return None
    if data is None:
        if last_err is not None:
            logging.warning("status fetch failed across all SSL strategies: %s",
                            last_err)
        return None
    try:
        for c in data.get("components", []):
            comp_name = (c.get("name") or "").strip().lower()
            if comp_name == "claude.ai":
                key = c.get("status") or "operational"
                label = STATUS_COLORS.get(key, (key.replace("_", " ").title(), None))[0]
                return (key, label)
    except Exception:
        logging.exception("status parse failed")
    return None


# ---------- version / update check ----------

def _parse_version(s: str) -> Tuple[int, ...]:
    """Parse '1.2.3' (or 'v1.2.3') to (1,2,3). Non-numeric parts become 0.
    Empty -> (0,)."""
    s = (s or "").strip().lstrip("vV")
    s = s.split()[0] if s else ""
    out = []
    for piece in s.split("."):
        try:
            out.append(int("".join(ch for ch in piece if ch.isdigit())))
        except ValueError:
            out.append(0)
    return tuple(out) if out else (0,)


def _promote_tray_icon() -> int:
    """Set IsPromoted=1 on every NotifyIconSettings entry whose ExecutablePath
    matches our exe (basename match, case-insensitive). Win11 defaults new
    tray icons to IsPromoted=0 ("Other system tray icons" toggle = Off), so
    fresh installs land the icon in the overflow flyout. Flipping the value
    here + broadcasting WM_SETTINGCHANGE asks Explorer to re-evaluate the
    tray layout without a full Explorer restart.

    Note: Explorer often won't pick up a mid-session IsPromoted change for
    an already-registered icon; the value sticks for next launch but the
    current session's tray layout is fixed. The accompanying log lines
    diagnose what entries exist and which were touched.

    Gated on sys.frozen so dev runs don't accidentally promote python.exe."""
    if sys.platform != "win32":
        return 0
    if not getattr(sys, "frozen", False):
        return 0
    try:
        import winreg
        import ctypes
    except ImportError:
        return 0

    our_exe_full = os.path.normcase(os.path.abspath(sys.executable))
    our_exe_base = os.path.basename(our_exe_full).lower()  # "claude usage.exe"
    root_path = r"Control Panel\NotifyIconSettings"
    promoted = 0
    seen = 0

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path,
                            0, winreg.KEY_READ) as root:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    sub_path = root_path + "\\" + sub_name
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_path, 0,
                                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as sub:
                        try:
                            ep, _ = winreg.QueryValueEx(sub, "ExecutablePath")
                        except FileNotFoundError:
                            continue
                        if not isinstance(ep, str) or not ep:
                            continue
                        try:
                            ep_expanded = os.path.expandvars(ep)
                            ep_base = os.path.basename(ep_expanded).lower()
                        except Exception:
                            continue
                        seen += 1
                        # Match by filename only -- path normalization across
                        # registry storage formats (DOS short, env-var-expanded,
                        # \\?\, etc.) is too fragile for full-path matching.
                        if ep_base != our_exe_base:
                            continue
                        try:
                            cur, _ = winreg.QueryValueEx(sub, "IsPromoted")
                        except FileNotFoundError:
                            cur = 0
                        logging.info(
                            "tray entry %s ExecutablePath=%r IsPromoted(was)=%s",
                            sub_name, ep, cur)
                        if cur != 1:
                            winreg.SetValueEx(sub, "IsPromoted", 0,
                                              winreg.REG_DWORD, 1)
                            promoted += 1
                            logging.info("  -> set IsPromoted=1 on %s", sub_name)
                        else:
                            logging.info("  -> already promoted, no change")
                except OSError:
                    continue
    except FileNotFoundError:
        logging.info("_promote_tray_icon: no NotifyIconSettings root present")
        return 0
    except Exception:
        logging.exception("_promote_tray_icon walk failed")
        return 0

    logging.info(
        "_promote_tray_icon: scanned=%d promoted=%d our_exe=%r",
        seen, promoted, our_exe_full)

    if promoted:
        try:
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            res = ctypes.c_ulong(0)
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                ctypes.c_wchar_p("TraySettings"),
                SMTO_ABORTIFHUNG, 1000, ctypes.byref(res))
        except Exception:
            logging.exception("WM_SETTINGCHANGE broadcast failed")
    return promoted


def fetch_latest_version() -> Optional[str]:
    """Fetch the latest release tag from GitHub. Returns just the version
    string (e.g. '1.1.0', without leading 'v') or None on failure."""
    if not UPDATE_VERSION_URL:
        return None
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.get(
                UPDATE_VERSION_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    # GitHub asks for a User-Agent on API requests:
                    "User-Agent": f"claude-usage-widget/{__version__}",
                },
            )
        if r.status_code != 200:
            logging.info("update check: HTTP %s from %s", r.status_code, UPDATE_VERSION_URL)
            return None
        data = r.json()
        # Skip drafts and pre-releases (defensive; releases/latest already filters).
        if data.get("draft") or data.get("prerelease"):
            return None
        tag = data.get("tag_name") or ""
        return tag.lstrip("vV") or None
    except Exception:
        logging.exception("update check failed")
    return None


# ---------- toast notifications ----------

def show_toast(title: str, msg: str) -> None:
    """Pop a Windows toast. Best-effort; silently no-ops on non-Windows or if winotify isn't installed."""
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification  # type: ignore
        icon_candidates = [
            Path(getattr(sys, "_MEIPASS", "")) / "app.ico",
            Path(__file__).parent / "app.ico",
        ]
        icon_path = next((p for p in icon_candidates if p.exists()), None)
        toast = Notification(
            app_id=APP_DISPLAY_NAME,
            title=title,
            msg=msg,
            icon=str(icon_path) if icon_path else "",
        )
        toast.show()
    except Exception:
        logging.exception("toast failed")


# ---------- claude.ai usage fetcher ----------

class ClaudeUsageFetcher:
    BASE = "https://claude.ai"
    USAGE_PATH = "/api/organizations/{org}/usage"
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    SESSION_KEYS = ("five_hour", "current_session", "session")
    WEEKLY_KEYS = ("seven_day", "weekly", "week")

    def __init__(self, cookies, user_agent="", extra_headers=None, org_id=""):
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
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        for k, v in (extra_headers or {}).items():
            if k.lower() in ("cookie", "host", "content-length"):
                continue
            headers[k] = v
        self.client = httpx.Client(headers=headers, cookies=self.cookies, timeout=20.0, follow_redirects=True)

    def close(self):
        try: self.client.close()
        except Exception: pass

    def discover_org_id(self):
        try:
            r = self.client.get(f"{self.BASE}/api/organizations")
            if r.status_code != 200:
                logging.warning("/api/organizations -> %s", r.status_code)
                return None
            data = r.json()
            if isinstance(data, list) and data:
                first = data[0]
                org_id = first.get("uuid") or first.get("id")
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
                logging.warning("Could not determine org_id - cookies may be invalid")
                return None
        url = self.BASE + self.USAGE_PATH.format(org=self.org_id)
        try:
            r = self.client.get(url)
        except Exception:
            logging.exception("usage request error")
            return None
        if r.status_code != 200:
            logging.info("usage endpoint -> %s body=%s", r.status_code, r.text[:300].replace("\n", " "))
            return None
        try:
            data = r.json()
        except Exception:
            logging.exception("usage response not JSON: %s", r.text[:300])
            return None
        return self._extract_percents(data)

    @classmethod
    def _extract_percents(cls, data):
        """Return (session_pct, weekly_pct, session_resets_at, weekly_resets_at)
        on success, or None. The reset strings are raw ISO 8601 from the API
        (or '' if absent)."""
        def to_pct(v):
            try: f = float(v)
            except (TypeError, ValueError): return None
            # Claude.ai's API always reports utilization on a 0-100 scale
            # (e.g. 52.0 means 52%). Don't try to interpret < 1.0 as a
            # fraction -- that produces a false 100% for 1% real usage.
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
            logging.warning("extract_percents incomplete: session=%s weekly=%s", s, w)
            return None
        logging.debug("usage parsed: session=%.1f weekly=%.1f", s, w)
        return (s, w, s_reset, w_reset)


# ---------- activity watcher ----------

class ActivityWatcher(threading.Thread):
    """Watches the Claude desktop app's data folders for file changes that
    indicate the user just sent or received a message, then triggers a
    fetch (debounced + rate-limited).

    The Claude desktop app writes to LevelDB / IndexedDB / Session Storage
    on every interaction, so we can use the directory mtimes as a free
    "user just used Claude" signal without polling the API constantly.
    """

    DEBOUNCE_SEC = 3.0       # wait this long after the last change
    MIN_INTERVAL_SEC = 30.0  # don't trigger more than once per N sec
    POLL_SEC = 2.0           # how often to scan the watched dirs

    def __init__(self, on_activity, stop_evt: threading.Event) -> None:
        super().__init__(daemon=True, name="ActivityWatcher")
        self.on_activity = on_activity
        self.stop_evt = stop_evt
        self.watch_dirs = self._discover_dirs()
        self.last_seen = self._snapshot()
        self.last_triggered = 0.0
        self.pending_until = 0.0
        if self.watch_dirs:
            logging.info("activity watcher: %d dir(s) - %s",
                         len(self.watch_dirs),
                         ", ".join(str(d) for d in self.watch_dirs))
        else:
            logging.info("activity watcher: no Claude desktop data dirs found; relying on poll only")

    @staticmethod
    def _discover_dirs():
        roots: List[Path] = []
        appdata = Path(os.environ.get("APPDATA", ""))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
        for base in (appdata, localappdata):
            if base:
                roots.append(base / "Claude")
        # Microsoft Store / UWP install
        if localappdata:
            packages_dir = localappdata / "Packages"
            if packages_dir.is_dir():
                try:
                    for pkg in packages_dir.glob("Claude_*"):
                        roots.append(pkg / "LocalCache" / "Roaming" / "Claude")
                except OSError:
                    pass
        candidates: List[Path] = []
        for r in roots:
            candidates += [
                r / "Local Storage" / "leveldb",
                r / "IndexedDB",
                r / "Session Storage",
                r / "Network",
            ]
        return [d for d in candidates if d.is_dir()]

    def _snapshot(self):
        out = {}
        for d in self.watch_dirs:
            try:
                for p in d.iterdir():
                    try:
                        out[str(p)] = p.stat().st_mtime
                    except OSError:
                        pass
            except OSError:
                pass
        return out

    def run(self) -> None:
        while not self.stop_evt.is_set():
            now = time.time()
            snap = self._snapshot()
            if snap != self.last_seen:
                self.last_seen = snap
                self.pending_until = now + self.DEBOUNCE_SEC
            elif self.pending_until and now >= self.pending_until:
                self.pending_until = 0.0
                if now - self.last_triggered >= self.MIN_INTERVAL_SEC:
                    self.last_triggered = now
                    logging.debug("activity watcher: detected Claude activity, triggering fetch")
                    try:
                        self.on_activity()
                    except Exception:
                        logging.exception("on_activity callback failed")
            self.stop_evt.wait(self.POLL_SEC)


# ---------- localhost listener for the browser extension ----------

BRIDGE_PORT = 38080
# Hard cap on POST body size. The legitimate extension payload is a small
# JSON object (well under 4 KiB). Anything larger is either a bug or a
# malicious local app trying to OOM us by setting an oversized
# Content-Length and streaming. 64 KiB leaves plenty of headroom for the
# real payload and any future expansion without exposing us to abuse.
MAX_BRIDGE_BODY_BYTES = 64 * 1024


class _BridgeHandler(http.server.BaseHTTPRequestHandler):
    """Receives POSTs from the Claude Usage Bridge browser extension.

    Bound to 127.0.0.1 only -- nothing is exposed to the network. We also
    require an Origin header that begins with chrome-extension:// or
    moz-extension:// so a casually-malicious local app can't push fake
    values. Note that Origin headers are trivially spoofable by a native
    local process; a future release will move to a shared-secret design
    written by the installer and read by the extension on first run.
    """
    tray_app: "Optional[TrayApp]" = None  # set before serve_forever()

    def _accept_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    def _set_cors(self) -> None:
        # The extension's preflight will check these.
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/usage":
            self.send_response(404); self.end_headers(); return
        if not self._accept_origin():
            self.send_response(403); self.end_headers(); return
        # Validate Content-Length BEFORE reading the body. Reject negative,
        # malformed, or oversized lengths so we don't cooperate with an
        # attacker's allocation request.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.send_response(400); self.end_headers(); return
        if length < 0 or length > MAX_BRIDGE_BODY_BYTES:
            self.send_response(413); self.end_headers(); return
        # Be strict about Content-Type for any non-empty body -- the legit
        # extension always sends application/json.
        if length > 0:
            ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            if ctype and ctype != "application/json":
                self.send_response(415); self.end_headers(); return
        try:
            body = self.rfile.read(length) if length else b""
            data = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400); self.end_headers(); return
        except Exception:
            logging.exception("bridge handler body read failed")
            self.send_response(400); self.end_headers(); return
        try:
            if self.__class__.tray_app is not None:
                self.__class__.tray_app.handle_extension_usage(data)
        except Exception:
            logging.exception("bridge handler failed")
            self.send_response(500); self._set_cors(); self.end_headers(); return
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    # Suppress the default per-request stderr line so the log stays clean.
    def log_message(self, format, *args):  # noqa: A003 - matches stdlib API
        return


def start_bridge_listener(tray_app: "TrayApp") -> Optional[socketserver.TCPServer]:
    """Spin up the localhost listener on a background thread. Returns the
    server (so callers can shut it down on exit) or None if the port is busy.
    """
    _BridgeHandler.tray_app = tray_app
    try:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", BRIDGE_PORT), _BridgeHandler)
    except OSError as e:
        logging.warning("bridge listener: port %d in use (%s); extension input disabled",
                        BRIDGE_PORT, e)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="BridgeListener").start()
    logging.info("bridge listener: listening on 127.0.0.1:%d", BRIDGE_PORT)
    return server


# ---------- the tray app ----------

class TrayApp:
    # If the browser extension pushed within this many seconds, the local poll
    # loop and activity watcher should defer to it instead of overwriting the
    # source label with the manual-paste fallback.
    EXTENSION_FRESHNESS_SEC = 90

    def __init__(self):
        self.config = load_config()
        self.session_pct = float(self.config.get("session_pct", 0))
        self.weekly_pct = float(self.config.get("weekly_pct", 0))
        self.icon = None
        self.stop_evt = threading.Event()
        self.last_status = "Starting..."
        self.last_extension_push_at = 0.0  # epoch seconds; 0 = never
        self.claude_status_key = "operational"
        self.claude_status_label = "All systems operational"
        self.latest_version: Optional[str] = None  # None = not yet checked

    def _icon_image(self):
        dot = STATUS_COLORS.get(self.claude_status_key, (None, None))[1]
        return render_icon(self.session_pct, self.weekly_pct, size=64, status_dot=dot)

    def _status_poll_loop(self) -> None:
        """Every 5 minutes, check status.claude.com for the claude.ai
        component status and update our icon dot. Toast on transitions both
        ways (operational -> not, and back to operational)."""
        first = True
        while not self.stop_evt.is_set():
            try:
                res = fetch_claude_status()
                if res is not None:
                    new_key, new_label = res
                    prev_key = self.claude_status_key
                    self.claude_status_key = new_key
                    self.claude_status_label = new_label
                    if first:
                        logging.info("claude.ai status: %s", new_key)
                    elif prev_key != new_key:
                        logging.info("claude.ai status change: %s -> %s",
                                     prev_key, new_key)
                        if prev_key == "operational" and new_key != "operational":
                            # Things just went sideways.
                            show_toast(
                                "Claude.ai status: " + new_label,
                                f"status.claude.com is reporting: {new_label}.",
                            )
                        elif prev_key != "operational" and new_key == "operational":
                            # Recovery.
                            show_toast(
                                "Claude.ai is back to normal",
                                "status.claude.com now reports all systems operational.",
                            )
                    self._refresh_icon()
                first = False
            except Exception:
                logging.exception("status poll error")
            self.stop_evt.wait(300)  # 5 minutes

    def action_open_status_page(self, icon, item) -> None:
        try:
            os.startfile(CLAUDE_STATUS_URL)
        except Exception:
            logging.exception("open status page failed")

    def action_check_updates(self, icon, item) -> None:
        """Open the GitHub Releases page in the user's default browser so
        they can grab the latest installer. Always available even when no
        newer version has been detected, so users can share the current
        installer with friends."""
        try:
            os.startfile(UPDATE_DOWNLOAD_URL)
        except Exception:
            logging.exception("open releases page failed")

    def _update_check_loop(self) -> None:
        """On startup and once per 24h, check GitHub Releases for a newer
        version. On failure (network blip, GitHub down, parse error) retry
        in 1 hour instead of waiting the full 24h, so an early-morning
        blip doesn't push the next attempt out by a whole day.
        Toast once per detected new version per session."""
        notified_for: Optional[str] = None
        while not self.stop_evt.is_set():
            ok = False
            try:
                latest = fetch_latest_version()
                if latest:
                    ok = True
                    self.latest_version = latest
                    if (_parse_version(latest) > _parse_version(__version__)
                            and notified_for != latest):
                        show_toast(
                            f"Claude Usage update available: v{latest}",
                            f"You're on v{__version__}. Right-click the tray icon "
                            f"to download v{latest}.",
                        )
                        logging.info("update available: installed=%s, latest=%s",
                                     __version__, latest)
                        notified_for = latest
                    self._refresh_icon()
            except Exception:
                logging.exception("update_check_loop error")
            # Long wait on success, short retry on failure.
            self.stop_evt.wait(24 * 3600 if ok else 3600)

    def _tooltip(self):
        # Windows caps tray tooltips at 128 characters via Shell_NotifyIcon's
        # szTip[]. We aim well below that with abbreviated labels and truncate
        # defensively at the end.
        last_source = self.config.get("last_source") or "-"
        # Abbreviate the source for the tooltip (full name still in the menu).
        src_short = {
            "Claude browser extension": "browser ext.",
            "Claude desktop app": "desktop app",
            "Manual paste": "manual paste",
        }.get(last_source, last_source)

        if last_source == "Claude browser extension" and self.last_extension_push_at > 0:
            gap = max(0.0, time.time() - self.last_extension_push_at)
            if gap < 60:
                src_short += f" ({int(gap)}s ago)"
            elif gap < 3600:
                src_short += f" ({int(gap / 60)}m ago)"
            else:
                src_short += f" ({int(gap / 3600)}h ago)"

        s_reset = fmt_reset(self.config.get("session_resets_at", ""), short=True)
        w_reset = fmt_reset(self.config.get("weekly_resets_at", ""), short=False)
        s_line = f"Session: {self.session_pct:.0f}%"
        if s_reset:
            s_line += f"  ({s_reset})"
        elif self.session_pct < 0.5:
            # No active 5-hour window yet -- the API only sets resets_at after
            # the user's first message of the session.
            s_line += "  (idle)"
        w_line = f"Weekly:  {self.weekly_pct:.0f}%"
        if w_reset:
            w_line += f"  ({w_reset})"

        text = f"{s_line}\n{w_line}\nSource: {src_short}\n{self.last_status}"
        # Defensive cap: never let pystray throw a ValueError.
        if len(text) > 127:
            text = text[:124] + "..."
        return text

    def _refresh_icon(self):
        if self.icon:
            self.icon.icon = self._icon_image()
            self.icon.title = self._tooltip()
            # Force pystray to re-evaluate the lambda labels in _build_menu,
            # otherwise Windows shows stale Session/Weekly/Source/Status text.
            try:
                self.icon.update_menu()
            except Exception:
                logging.exception("update_menu failed")

    def _try_auto_cookies(self) -> Optional[Tuple[str, Dict[str, str], str]]:
        try:
            return cookie_sources.fetch_cookies()
        except Exception:
            logging.exception("auto-cookie source failed")
            return None

    def _build_fetcher(self) -> Tuple[Optional[ClaudeUsageFetcher], str]:
        """Return (fetcher, source_label). If auto_refresh is on, prefer
        live cookies from the desktop app / browsers. Otherwise use the
        cookies stored in config (from last cURL paste)."""
        if self.config.get("auto_refresh", True) and self.config.get("mode") in ("auto", "scrape"):
            res = self._try_auto_cookies()
            if res is not None:
                source_label, cookies, ua = res
                return (
                    ClaudeUsageFetcher(
                        cookies=cookies, user_agent=ua,
                        extra_headers={}, org_id=self.config.get("org_id", ""),
                    ),
                    source_label,
                )
        # Fallback: stored cookies (cURL paste)
        cookies = dict(self.config.get("cookies") or {})
        if not cookies and self.config.get("session_cookie"):
            cookies = {"sessionKey": self.config["session_cookie"]}
        if not cookies:
            return (None, "")
        return (
            ClaudeUsageFetcher(
                cookies=cookies,
                user_agent=self.config.get("user_agent", ""),
                extra_headers=self.config.get("extra_headers") or {},
                org_id=self.config.get("org_id", ""),
            ),
            "Manual paste",
        )

    def _maybe_notify(self, title: str, msg: str) -> None:
        if not self.config.get("notify_on_failure", True):
            return
        # Throttle: at most one toast per hour
        last = float(self.config.get("last_failure_notified_at", 0) or 0)
        if time.time() - last < 3600:
            return
        self.config["last_failure_notified_at"] = time.time()
        save_config(self.config)
        show_toast(title, msg)

    def _poll_loop(self):
        while not self.stop_evt.is_set():
            try:
                self._fetch_once()
            except Exception:
                logging.exception("poll loop error")
            interval = max(30, int(self.config.get("poll_seconds", 60)))
            self.stop_evt.wait(interval)

    def _fetch_once(self):
        # If the browser extension is actively pushing data, defer to it.
        # The extension's view is fresher and avoids the source-label flap
        # caused by our local fetch falling back to the manual-paste cookies.
        if self._extension_is_fresh():
            return
        fetcher, source_label = self._build_fetcher()
        if fetcher is None:
            self.last_status = "Open Claude desktop app and sign in"
            self._maybe_notify(
                "Claude Usage",
                "Sign into the Claude desktop app (https://claude.ai/download) so the widget can read your usage.",
            )
            self._refresh_icon()
            return
        try:
            res = fetcher.fetch()
            if res:
                self.session_pct, self.weekly_pct, s_reset, w_reset = res
                self.config["session_pct"] = self.session_pct
                self.config["weekly_pct"] = self.weekly_pct
                self.config["session_resets_at"] = s_reset
                self.config["weekly_resets_at"] = w_reset
                prev_source = self.config.get("last_source", "")
                if source_label and source_label != prev_source:
                    logging.info("source changed: %r -> %r",
                                 prev_source or "(none)", source_label)
                self.config["last_source"] = source_label
                if fetcher.org_id and not self.config.get("org_id"):
                    self.config["org_id"] = fetcher.org_id
                save_config(self.config)
                self.last_status = f"Updated {fmt_time_12h()}"
            else:
                self.last_status = f"Refresh failed at {fmt_time_12h()}"
                self._maybe_notify(
                    "Claude Usage - sign-in needed",
                    "Couldn't read usage. Open the Claude desktop app and make sure you're signed in.",
                )
                if fetcher.org_id and not self.config.get("org_id"):
                    self.config["org_id"] = fetcher.org_id
                    save_config(self.config)
        finally:
            fetcher.close()
        self._refresh_icon()

    # ----- menu actions -----
    def _check_status_once(self) -> None:
        """One-shot status fetch + icon refresh. Used by action_refresh so
        the user can force a status update without waiting for the 5-minute
        poll loop."""
        try:
            res = fetch_claude_status()
            if res is not None:
                self.claude_status_key, self.claude_status_label = res
                self._refresh_icon()
        except Exception:
            logging.exception("manual status check failed")

    def action_refresh(self, icon, item):
        threading.Thread(target=self._fetch_once, daemon=True).start()
        threading.Thread(target=self._check_status_once, daemon=True).start()

    def action_toggle_auto(self, icon, item):
        self.config["auto_refresh"] = not bool(self.config.get("auto_refresh", True))
        if self.config["auto_refresh"] and self.config.get("mode") == "manual":
            self.config["mode"] = "auto"
        save_config(self.config)
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def action_toggle_notify(self, icon, item):
        self.config["notify_on_failure"] = not bool(self.config.get("notify_on_failure", True))
        save_config(self.config)
        self._refresh_icon()

    def action_paste_curl(self, icon, item):
        def go():
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            top = tk.Toplevel(root)
            top.title("Paste cURL command from claude.ai")
            top.attributes("-topmost", True)
            top.geometry("720x440")
            instr = (
                "Optional manual fallback. The recommended setup is to install\n"
                "the Claude desktop app from https://claude.ai/download instead --\n"
                "the widget reads its cookies automatically.\n\n"
                "To paste cURL anyway:\n"
                "1. Open https://claude.ai/settings/usage in any signed-in browser.\n"
                "2. Press F12 -> Network tab -> Fetch/XHR filter.\n"
                "3. Reload. Right-click any /api/... request -> Copy -> Copy as cURL (bash).\n"
                "4. Paste below and click OK."
            )
            tk.Label(top, text=instr, justify="left", padx=10, pady=8).pack(anchor="w")
            txt = tk.Text(top, wrap="word", height=15)
            txt.pack(fill="both", expand=True, padx=10, pady=4)
            result = {"ok": False, "value": ""}
            def on_ok():
                result["ok"] = True
                result["value"] = txt.get("1.0", "end").strip()
                top.destroy(); root.destroy()
            def on_cancel():
                top.destroy(); root.destroy()
            btns = tk.Frame(top); btns.pack(fill="x", pady=8)
            tk.Button(btns, text="OK", width=10, command=on_ok).pack(side="right", padx=8)
            tk.Button(btns, text="Cancel", width=10, command=on_cancel).pack(side="right")
            top.protocol("WM_DELETE_WINDOW", on_cancel)
            top.grab_set()
            root.wait_window(top)
            if not result["ok"] or not result["value"]:
                return
            headers, cookies = parse_curl_command(result["value"])
            if not cookies:
                logging.warning("paste_curl: no cookies parsed")
                self.last_status = "cURL paste: no cookies found"
                self._refresh_icon()
                return
            ua = headers.pop("User-Agent", "") or headers.pop("user-agent", "")
            extra = {k: v for k, v in headers.items() if not k.startswith(":")}
            self.config["cookies"] = cookies
            self.config["user_agent"] = ua
            self.config["extra_headers"] = extra
            self.config["session_cookie"] = ""
            self.config["mode"] = "scrape"
            self.config["org_id"] = ""
            save_config(self.config)
            logging.info("paste_curl: %d cookies, UA=%r, %d extra headers", len(cookies), ua, len(extra))
            self._fetch_once()
        threading.Thread(target=go, daemon=True).start()

    def action_set_manual(self, icon, item):
        # Disabled: kept as a stub so older configs that referenced this don't crash.
        pass

    def action_open_log(self, icon, item):
        try:
            os.startfile(str(LOG_FILE))
        except Exception:
            logging.exception("open log failed")

    def action_open_tray_settings(self, icon, item):
        try:
            os.startfile("ms-settings:taskbar")
        except Exception:
            logging.exception("could not open ms-settings:taskbar")

    def action_quit(self, icon, item):
        self.stop_evt.set()
        if self.icon: self.icon.stop()

    def _version_menu_label(self) -> str:
        if self.latest_version and _parse_version(self.latest_version) > _parse_version(__version__):
            return f"Update available: v{self.latest_version}  (click to download)"
        return f"Claude Usage v{__version__}  (click to download folder)"

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Session: {self.session_pct:.0f}%   Weekly: {self.weekly_pct:.0f}%",
                None, enabled=False),
            pystray.MenuItem(
                lambda item: f"Source: {self.config.get('last_source') or '-'}",
                None, enabled=False),
            pystray.MenuItem(
                lambda item: f"Claude.ai: {self.claude_status_label}",
                None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh now", self.action_refresh, default=True),
            pystray.MenuItem("Open Claude status page", self.action_open_status_page),
            pystray.MenuItem(lambda item: self._version_menu_label(),
                             self.action_check_updates),
            pystray.MenuItem(
                "Auto-refresh from Claude desktop app",
                self.action_toggle_auto,
                checked=lambda item: bool(self.config.get("auto_refresh", True))),
            pystray.MenuItem(
                "Notify on failure",
                self.action_toggle_notify,
                checked=lambda item: bool(self.config.get("notify_on_failure", True))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Paste cURL command (fallback)...", self.action_paste_curl),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Windows tray icon settings", self.action_open_tray_settings),
            pystray.MenuItem("Open log folder", self.action_open_log),
            pystray.MenuItem("Quit", self.action_quit),
        )

    def _on_activity(self) -> None:
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def handle_extension_usage(self, data: dict) -> None:
        """Apply usage data POSTed from the browser extension."""
        pcts = ClaudeUsageFetcher._extract_percents(data)
        if pcts is None:
            logging.warning("bridge: extension payload didn't parse: keys=%r",
                            list(data.keys())[:10] if isinstance(data, dict) else type(data))
            return
        s, w, s_reset, w_reset = pcts
        self.session_pct = s
        self.weekly_pct = w
        self.config["session_pct"] = s
        self.config["weekly_pct"] = w
        self.config["session_resets_at"] = s_reset
        self.config["weekly_resets_at"] = w_reset
        prev = self.config.get("last_source", "")
        new_src = "Claude browser extension"
        if new_src != prev:
            logging.info("source changed: %r -> %r", prev or "(none)", new_src)
        self.config["last_source"] = new_src
        self.last_extension_push_at = time.time()
        self.last_status = f"Updated {fmt_time_12h()}"
        save_config(self.config)
        self._refresh_icon()

    def _extension_is_fresh(self) -> bool:
        return (time.time() - self.last_extension_push_at) < self.EXTENSION_FRESHNESS_SEC

    def _tooltip_tick_loop(self) -> None:
        """Refresh the tooltip every ~10s so the 'Xs ago' counter keeps
        moving while the browser extension is the active source. Skips
        the work when the source isn't the extension (manual paste
        tooltips don't need re-rendering between fetches)."""
        while not self.stop_evt.is_set():
            if (self.icon is not None
                    and self.config.get("last_source") == "Claude browser extension"):
                try:
                    self.icon.title = self._tooltip()
                except Exception:
                    pass
            self.stop_evt.wait(10)

    def _promote_tray_loop(self) -> None:
        """Wait for Windows to materialize the NotifyIconSettings entry,
        then promote it. Retry up to 3 times 5s apart since the entry can
        take a moment to appear after Shell_NotifyIcon NIM_ADD. Stops on
        the first pass that actually flipped IsPromoted, so a healthy
        install doesn't burn three log entries when one would do.

        Mid-session promotion does not actually move an already-registered
        icon to the visible tray; the value sticks for next launch. The
        installer's orphan-only cleanup ensures that next-launch state
        survives upgrades.

        Note: a zero return from _promote_tray_icon() means either 'entry
        not yet present' or 'entry already promoted' (the function
        doesn't distinguish). Retries cover the slow-NIM_ADD case; a
        machine where the entry is already promoted just sees three
        debug-level passes that find nothing to do."""
        for attempt in range(3):
            self.stop_evt.wait(5)
            if self.stop_evt.is_set():
                return
            try:
                # Idempotent: extra runs see IsPromoted=1 and skip writes.
                promoted = _promote_tray_icon()
            except Exception:
                logging.exception("promote_tray_icon failed")
                continue
            if promoted > 0:
                # First pass that actually fixed something; no need to retry.
                return

    def run(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._tooltip_tick_loop, daemon=True).start()
        threading.Thread(target=self._status_poll_loop, daemon=True).start()
        threading.Thread(target=self._update_check_loop, daemon=True).start()
        threading.Thread(target=self._promote_tray_loop, daemon=True).start()
        ActivityWatcher(self._on_activity, self.stop_evt).start()
        start_bridge_listener(self)
        self.icon = pystray.Icon(APP_DISPLAY_NAME, icon=self._icon_image(),
                                 title=self._tooltip(), menu=self._build_menu())
        self.icon.run()


def main():
    _set_app_user_model_id()
    try:
        TrayApp().run()
    except Exception:
        logging.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
