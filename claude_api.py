"""Shared HTTP / version / status helpers used by both the Windows tray
(claude_usage_tray.py) and the macOS menu bar (claude_usage_menubar.py)
entry scripts.

Why this module exists:
  Before v1.3.0 these helpers were duplicated -- byte-for-byte in some
  cases, with subtle drift in others -- between the two entry scripts.
  A bug fix to JSON parsing or to the TLS bootstrap had to land twice or
  one platform would silently miss it. Extracting the shared logic here
  means a single bug fix protects both platforms.

What lives here:
  - Constants: GitHub Releases URLs, Claude status page URL, the
    Statuspage component-status -> (label, dot color) map.
  - _setup_bundled_certifi(): TLS CA bundle resolution for PyInstaller
    bundles. Searches the same candidate paths on both platforms; the
    Mac-specific .app/Contents/Resources/... path is included by default
    so the helper works for both layouts.
  - _status_ssl_strategies() + fetch_claude_status(): Statuspage fetch
    with three-level SSL fallback (default -> certifi -> unverified).
    The unverified fallback is acceptable here because the data is
    public and read-only; do NOT reuse this pattern for cookie traffic.
  - _parse_version() + fetch_latest_version(): GitHub Releases API
    polling for the in-app "update available" toast.
  - ClaudeUsageFetcher: the authenticated claude.ai usage API client.
    Takes the platform-specific User-Agent + sec-ch-ua-platform hint
    as constructor args so both entry scripts share one implementation.

What does NOT live here:
  Anything platform-specific: registry / pystray (Windows), rumps /
  osascript (Mac), Keychain access, the browser-extension HTTP bridge.
  Those stay in their respective entry script or platform-specific
  helper module.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

import httpx


# ---------- public constants ----------

UPDATE_VERSION_URL = (
    "https://api.github.com/repos/diggystyon/claude-usage-widget/releases/latest"
)
UPDATE_DOWNLOAD_URL = (
    "https://github.com/diggystyon/claude-usage-widget/releases/latest"
)

CLAUDE_STATUS_URL = "https://status.claude.com/"

# Maps Statuspage component status keys to (display label, dot RGB).
# 'operational' has no dot (suppressed when overlay color is None).
STATUS_COLORS: Dict[str, Tuple[str, Optional[Tuple[int, int, int]]]] = {
    "operational":          ("All systems operational", None),
    "degraded_performance": ("Degraded performance",    (255, 200, 0)),
    "partial_outage":       ("Partial outage",          (255, 140, 0)),
    "major_outage":         ("Major outage",            (235, 60, 60)),
    "under_maintenance":    ("Under maintenance",       (80, 140, 220)),
}


# ---------- TLS CA bundle resolution ----------

def _setup_bundled_certifi() -> None:
    """Make TLS verification work inside the PyInstaller bundle, on both
    Windows (.exe) and macOS (.app).

    PyInstaller's --collect-data certifi flag copies cacert.pem into the
    bundle, but at runtime certifi.where() can still return a path that
    doesn't resolve (depending on how the runtime hook fires). When that
    happens, httpx defaults fail with [Errno 2] and we silently slide to
    verify=False. Point SSL_CERT_FILE at a real on-disk cacert.pem
    before any httpx.Client is constructed.

    Search order (first match wins):
      1. an explicit SSL_CERT_FILE the user already set
      2. certifi.where() if the file exists
      3. {sys._MEIPASS}/certifi/cacert.pem  -- PyInstaller --onefile
      4. {exe_dir}/../Resources/certifi/cacert.pem  -- macOS .app layout
      5. {exe_dir}/certifi/cacert.pem  -- PyInstaller --onedir
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
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # macOS .app: Contents/MacOS/Claude Usage -> ../Resources/certifi/...
        candidates.append(os.path.join(exe_dir, "..", "Resources", "certifi", "cacert.pem"))
        # PyInstaller --onedir on either platform.
        candidates.append(os.path.join(exe_dir, "certifi", "cacert.pem"))
        for path in candidates:
            if not path:
                continue
            resolved = os.path.normpath(path)
            if os.path.isfile(resolved):
                os.environ["SSL_CERT_FILE"] = resolved
                os.environ["REQUESTS_CA_BUNDLE"] = resolved
                logging.info("TLS CA bundle: %s", resolved)
                return
        logging.warning("no CA bundle found; httpx will fall back to verify=False")
    except Exception:
        logging.exception("certifi bundle setup failed")


# ---------- claude.ai status page (Statuspage) ----------

def _status_ssl_strategies() -> List[Tuple[str, object]]:
    """Return a list of (name, verify-arg) tuples to try in order when
    talking to status.claude.com. PyInstaller's bundled ssl/certifi
    setup can fail with FileNotFoundError at first request, so we try
    the default, then certifi explicitly, then unverified as a last
    resort.

    The status endpoint serves public read-only data so verify=False is
    acceptable there. We do NOT use this for claude.ai cookie traffic.

    Note: previously this returned an httpx.Client and the caller
    cascaded on client-construction failures, but the constructor never
    raises -- SSL errors surface only at first request -- so the
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
    """Fetch the current 'claude.ai' component status from
    status.claude.com.

    Returns (status_key, label) like ('operational', 'All systems
    operational') or None on any failure (network, parse, missing
    component). Filters to ONLY the 'claude.ai' component, ignoring the
    API / Console / etc. components on the same Statuspage.

    Tries SSL strategies from _status_ssl_strategies() in order, falling
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
            logging.warning(
                "status fetch failed across all SSL strategies: %s", last_err)
        return None
    try:
        for c in data.get("components", []):
            comp_name = (c.get("name") or "").strip().lower()
            if comp_name == "claude.ai":
                key = c.get("status") or "operational"
                label = STATUS_COLORS.get(
                    key, (key.replace("_", " ").title(), None))[0]
                return (key, label)
    except Exception:
        logging.exception("status parse failed")
    return None


# ---------- GitHub release check ----------

def _parse_version(s: str) -> Tuple[int, ...]:
    """Parse '1.2.3' (or 'v1.2.3') to (1, 2, 3). Non-numeric parts
    become 0. Empty input -> (0,)."""
    s = (s or "").strip().lstrip("vV")
    s = s.split()[0] if s else ""
    out: List[int] = []
    for piece in s.split("."):
        try:
            out.append(int("".join(ch for ch in piece if ch.isdigit())))
        except ValueError:
            out.append(0)
    return tuple(out) if out else (0,)


def fetch_latest_version(user_agent: str) -> Optional[str]:
    """Fetch the latest non-draft, non-prerelease release tag from
    GitHub. Returns just the version string (e.g. '1.3.0', without
    leading 'v') or None on any failure.

    `user_agent` is required by GitHub's API and should be set to
    something like 'claude-usage-widget/1.3.0'.
    """
    if not UPDATE_VERSION_URL:
        return None
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.get(
                UPDATE_VERSION_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": user_agent,
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


# ---------- claude.ai usage API client ----------

class ClaudeUsageFetcher:
    """Authenticated claude.ai usage API client.

    Holds an httpx.Client preconfigured with the user's cookies plus the
    set of browser-y headers claude.ai expects (origin, referer, sec-ch
    hints). The platform-specific User-Agent and sec-ch-ua-platform are
    passed in by the caller, so this class works identically on Windows
    and macOS.

    Lifecycle: callers should `f = ClaudeUsageFetcher(...); try: f.fetch()
    finally: f.close()` to release the underlying connection.
    """

    BASE = "https://claude.ai"
    USAGE_PATH = "/api/organizations/{org}/usage"
    # Keys we look up under the API response root for session / weekly
    # utilization. Listed in order of preference: the API has shipped
    # several different shapes over time, and we accept any.
    SESSION_KEYS = ("five_hour", "current_session", "session")
    WEEKLY_KEYS  = ("seven_day", "weekly", "week")

    def __init__(self,
                 cookies: Optional[Dict[str, str]],
                 user_agent: str,
                 platform: str = "Windows",
                 extra_headers: Optional[Dict[str, str]] = None,
                 org_id: str = ""):
        """
        Args:
            cookies: name -> value. Must include sessionKey (and ideally
                the CF/lastActiveOrg cookies too).
            user_agent: the User-Agent header to send. Required.
            platform: one of "Windows", "macOS", "Linux". Becomes the
                sec-ch-ua-platform hint. Defaults to "Windows".
            extra_headers: optional dict of extra headers (used by the
                Windows tray's cURL-paste fallback to forward anything
                exotic the user's browser sent). Cookie / Host /
                Content-Length are stripped if present.
            org_id: known org_id, or empty to discover via
                /api/organizations on first fetch().
        """
        if not user_agent:
            raise ValueError("user_agent must be supplied by the caller")
        self.cookies = dict(cookies or {})
        self.org_id = (org_id or "").strip()
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://claude.ai",
            "Referer": "https://claude.ai/settings/billing",
            "sec-ch-ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{platform}"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        # Filter dangerous user-supplied headers. The cURL-paste path on
        # Windows passes whatever the user's browser sent, which may
        # include headers that would either confuse httpx (Cookie / Host
        # / Content-Length, all of which httpx manages itself) or
        # override our claude.ai auth posture (Authorization /
        # Origin / Referer) or trigger response encodings we can't decode
        # (Accept-Encoding: br when the brotli package isn't installed).
        # Blacklist over whitelist because the legitimate set is large
        # and browser-version-dependent; the dangerous set is short and
        # stable.
        _STRIPPED_HEADERS = frozenset((
            "cookie", "host", "content-length",
            "authorization", "origin", "referer", "accept-encoding",
        ))
        for k, v in (extra_headers or {}).items():
            if k.lower() in _STRIPPED_HEADERS:
                continue
            headers[k] = v
        self.client = httpx.Client(
            headers=headers, cookies=self.cookies,
            timeout=20.0, follow_redirects=True,
        )

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def discover_org_id(self) -> Optional[str]:
        """Call /api/organizations and return the first org's UUID, or
        None on any failure. Caller is responsible for caching."""
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

    def fetch(self) -> Optional[Tuple[float, float, str, str]]:
        """Fetch and parse the current usage. Returns
        (session_pct, weekly_pct, session_resets_at, weekly_resets_at)
        on success or None on any failure (no cookies, no org_id,
        non-200, non-JSON, no recognizable keys in the JSON).
        Reset strings are the raw ISO 8601 from the API, or "" if
        absent."""
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
            logging.info("usage endpoint -> %s body=%s",
                         r.status_code, r.text[:300].replace("\n", " "))
            return None
        try:
            data = r.json()
        except Exception:
            logging.exception("usage response not JSON: %s", r.text[:300])
            return None
        return self._extract_percents(data)

    @classmethod
    def _extract_percents(cls,
                          data: object
                          ) -> Optional[Tuple[float, float, str, str]]:
        """Pluck (session_pct, weekly_pct, session_resets_at,
        weekly_resets_at) out of the API response. Returns None if either
        percentage is missing.

        The API has shipped several response shapes over time. We try
        every key in SESSION_KEYS / WEEKLY_KEYS and accept the first
        that has a numeric 'utilization' in [0, 100].
        """
        def to_pct(v: object) -> Optional[float]:
            try:
                f = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            # Claude.ai's API always reports utilization on a 0-100
            # scale (e.g. 52.0 means 52%). Don't try to interpret < 1.0
            # as a fraction -- that produces a false 100% for 1% real
            # usage.
            if 0 <= f <= 100.0:
                return f
            return None

        s: Optional[float] = None
        w: Optional[float] = None
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
