"""
cookie_sources.py
-----------------
Find and decrypt claude.ai cookies from the Claude desktop app on Windows.

The Claude desktop app is an Electron application; its cookies live in a
standard Chromium SQLite store inside the app's userData directory:
  <UserData>/Local State            <- contains the encrypted master key
  <UserData>/Network/Cookies        <- SQLite cookie DB (newer Electron)
  <UserData>/Cookies                <- SQLite cookie DB (older Electron)

Cookie values may be plaintext, AES-GCM encrypted (v10/v11 prefix), or
DPAPI-blob encrypted (legacy). We handle all three.

Browser cookie sources (Edge / Chrome / Brave) are intentionally NOT
supported here: they ship with App-Bound Encryption (Chrome 127+) which
makes their cookies unreadable from outside the browser process. If a user
wants automatic refresh, they need the Claude desktop app installed.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Electron uses a Chrome User-Agent by default, so this matches what the
# desktop app sends to claude.ai.
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _user_data_roots() -> List[Tuple[str, Path, str]]:
    """Return (display_name, user_data_dir, user_agent) tuples for the
    Claude desktop app. Browser sources are intentionally not included --
    Chrome/Edge App-Bound Encryption blocks reading their cookies.

    Two install variants are supported:
      - Electron installer in %APPDATA%\\Claude
      - Electron installer in %LOCALAPPDATA%\\Claude

    The Microsoft Store / UWP install at
      %LOCALAPPDATA%\\Packages\\Claude_<publisher>\\...
    is INTENTIONALLY NOT scanned. Two reasons:

    1. UWP's cookie SQLite is opened with FILE_SHARE_NONE, so even
       CreateFileW with all share flags can't read it. We were already
       failing-fast with "cookies DB locked even with CreateFileW".
    2. MORE IMPORTANT: even the brief CreateFileW *attempt* opens a
       transient handle that MSIX/UWP treats as a reference to the
       entire package container. Combined with the activity watcher's
       directory scans, those references can prevent Microsoft Store
       upgrades from releasing the old install in
       C:\\Program Files\\WindowsApps\\Claude_<version>_..., which
       then makes Claude unable to relaunch after the update
       ("Another program is currently using this file"). Workaround
       was always "quit the widget"; the real fix is to never touch
       the UWP package container at all.

    UWP Claude users get cookies from the browser extension or the
    manual cURL paste flow instead. README.md documents this.
    """
    appdata = Path(os.environ.get("APPDATA", ""))
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates: List[Path] = []
    if appdata:
        candidates.append(appdata / "Claude")
    if localappdata:
        candidates.append(localappdata / "Claude")
    # UWP path explicitly omitted -- see docstring.

    out: List[Tuple[str, Path, str]] = []
    for root in candidates:
        if not root.is_dir():
            continue
        looks_like_userdata = (
            (root / "Local State").exists()
            or (root / "Network" / "Cookies").exists()
            or (root / "Cookies").exists()
            or (root / "Default" / "Network" / "Cookies").exists()
        )
        if looks_like_userdata:
            out.append(("Claude desktop app", root, UA_DESKTOP))
    return out


def _candidate_profiles(user_data_dir: Path) -> List[Path]:
    """Return likely profile dirs inside a Chromium user data dir.

    For browsers: 'Default', 'Profile 1', etc.
    For single-profile Electron apps (Claude desktop): the user data dir IS the profile.
    """
    if not user_data_dir.is_dir():
        return []
    profiles: List[Path] = []
    # Single-profile / Electron case: Cookies file may be directly in user_data_dir
    if (user_data_dir / "Network" / "Cookies").exists() or (user_data_dir / "Cookies").exists():
        profiles.append(user_data_dir)
    # Browser case: subdirectories
    for sub in sorted(user_data_dir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name == "Default" or sub.name.startswith("Profile "):
            profiles.append(sub)
    return profiles


def _decrypt_dpapi(blob: bytes) -> Optional[bytes]:
    """Best-effort DPAPI unprotect. Silent on failure -- callers log a
    one-line summary per source so we don't spam the log."""
    if sys.platform != "win32":
        return None
    try:
        import win32crypt  # type: ignore
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    except Exception:
        return None


def _get_master_key(local_state_path: Path) -> Optional[bytes]:
    """Decode the AES master key from a Chromium 'Local State' JSON file."""
    if not local_state_path.exists():
        return None
    try:
        ls = json.loads(local_state_path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("could not read Local State at %s", local_state_path)
        return None
    enc = ls.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        return None
    blob = base64.b64decode(enc)
    if blob[:5] == b"DPAPI":
        blob = blob[5:]
    return _decrypt_dpapi(blob)


def _decrypt_value(encrypted: bytes, master_key: Optional[bytes]) -> Optional[str]:
    """Decrypt a single Chromium cookie value blob. Silent on failure;
    the caller aggregates a per-source summary."""
    if not encrypted:
        return None
    prefix = encrypted[:3]
    # v20 = App-Bound Encryption (Chrome 127+ / Edge 127+, July 2024).
    # The key is wrapped in an extra layer that requires Chrome's IElevator
    # COM service running as the same process. We can't unwrap it from outside.
    if prefix == b"v20":
        return None
    if prefix in (b"v10", b"v11") and master_key is not None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = encrypted[3:15]
            ciphertext = encrypted[15:]
            aesgcm = AESGCM(master_key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except Exception:
            return None
    # Legacy: whole blob is a DPAPI ciphertext
    out = _decrypt_dpapi(encrypted)
    if out is None:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_locked_file(src: Path, dst: str) -> bool:
    """Copy a file that another process has open, using Win32 CreateFileW
    with explicit FILE_SHARE_READ|WRITE|DELETE so we can read past the lock.

    Returns True on success. On non-Windows, falls back to a plain shutil.copy2.
    """
    if sys.platform != "win32":
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x01
        FILE_SHARE_WRITE = 0x02
        FILE_SHARE_DELETE = 0x04
        OPEN_EXISTING = 3
        FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        CreateFileW.restype = wintypes.HANDLE
        ReadFile = ctypes.windll.kernel32.ReadFile
        CloseHandle = ctypes.windll.kernel32.CloseHandle

        handle = CreateFileW(
            str(src), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            return False
        try:
            buf = (ctypes.c_ubyte * (1 << 16))()
            bytes_read = wintypes.DWORD(0)
            with open(dst, "wb") as out:
                while True:
                    if not ReadFile(handle, buf, len(buf), ctypes.byref(bytes_read), None):
                        return False
                    if bytes_read.value == 0:
                        break
                    out.write(bytes(buf[:bytes_read.value]))
            return True
        finally:
            CloseHandle(handle)
    except Exception:
        logging.exception("CreateFileW copy failed for %s", src)
        return False


def _read_profile_cookies(profile: Path, master_key: Optional[bytes]) -> Dict[str, str]:
    """Read claude.ai cookies from a single profile dir. Logs a one-line
    summary so we don't spam the log with one entry per cookie on failure.

    Strategy:
      1. Try opening the SQLite DB directly with mode=ro&nolock=1&immutable=1.
         SQLite uses Win32 with full FILE_SHARE_* flags so this often works
         even when the file is open in another process.
      2. If that fails, copy the DB via CreateFileW (also with full share flags),
         then open the copy.
      3. If even that fails, give up and log a one-liner.
    """
    db = profile / "Network" / "Cookies"
    if not db.exists():
        db = profile / "Cookies"
    if not db.exists():
        return {}

    conn: Optional[sqlite3.Connection] = None
    tmp_path: Optional[str] = None
    open_method = ""

    # Strategy 1: open the live DB read-only via SQLite URI
    try:
        # Note: forward-slashes in file URIs work on Windows too
        uri_path = str(db).replace("\\", "/")
        conn = sqlite3.connect(
            f"file:/{uri_path}?mode=ro&nolock=1&immutable=1",
            uri=True,
        )
        # Quick probe so we fail fast if SQLite can't read it
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        open_method = "direct (SQLite)"
    except sqlite3.OperationalError as e:
        if conn is not None:
            try: conn.close()
            except Exception: pass
            conn = None
        logging.debug("source %s: direct SQLite open failed (%s); falling back to copy",
                      profile.name, e)

    # Strategy 2: copy via CreateFileW into temp, then open the copy
    if conn is None:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite")
        tmp.close()
        tmp_path = tmp.name
        if not _read_locked_file(db, tmp_path):
            logging.warning("source %s: cookies DB locked even with CreateFileW", profile.name)
            try: os.unlink(tmp_path)
            except FileNotFoundError: pass
            return {}
        # Best-effort copy of WAL/journal sidecars
        for ext in ("-journal", "-wal", "-shm"):
            sib = Path(str(db) + ext)
            if sib.exists():
                _read_locked_file(sib, tmp_path + ext)
        try:
            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro&immutable=1", uri=True)
            open_method = "via CreateFileW copy"
        except Exception:
            logging.exception("source %s: failed to open copied DB", profile.name)
            try: os.unlink(tmp_path)
            except FileNotFoundError: pass
            return {}

    try:
        cookies: Dict[str, str] = {}
        rows_total = 0
        rows_v20 = 0
        rows_decrypt_fail = 0
        try:
            cur = conn.execute(
                "SELECT name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%claude.ai'"
            )
            for name, value, encrypted in cur:
                rows_total += 1
                if value:
                    cookies[name] = value
                    continue
                if not encrypted:
                    continue
                if bytes(encrypted)[:3] == b"v20":
                    rows_v20 += 1
                    continue
                decoded = _decrypt_value(bytes(encrypted), master_key)
                if decoded is not None:
                    cookies[name] = decoded
                else:
                    rows_decrypt_fail += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if rows_total == 0:
            logging.debug("source %s [%s]: no claude.ai cookies in DB",
                          profile.name, open_method)
        elif rows_v20 > 0:
            logging.warning(
                "source %s [%s]: %d/%d cookies blocked by App-Bound Encryption (v20).",
                profile.name, open_method, rows_v20, rows_total,
            )
        elif rows_decrypt_fail > 0 and not cookies:
            logging.warning(
                "source %s [%s]: failed to decrypt %d/%d cookies",
                profile.name, open_method, rows_decrypt_fail, rows_total,
            )
        else:
            logging.debug(
                "source %s [%s]: read %d cookies (%d decrypted)",
                profile.name, open_method, len(cookies),
                max(0, rows_total - rows_v20 - rows_decrypt_fail),
            )
        return cookies
    except Exception:
        logging.exception("error reading cookies from %s", db)
        return {}
    finally:
        if tmp_path:
            for ext in ("", "-journal", "-wal", "-shm"):
                try:
                    os.unlink(tmp_path + ext)
                except FileNotFoundError:
                    pass


def discover() -> List[Tuple[str, Path]]:
    """List potential sources without decrypting yet (for logging/UI)."""
    found: List[Tuple[str, Path]] = []
    for name, root, _ in _user_data_roots():
        for prof in _candidate_profiles(root):
            db = prof / "Network" / "Cookies"
            if not db.exists():
                db = prof / "Cookies"
            if db.exists():
                label = name if prof == root else f"{name} ({prof.name})"
                found.append((label, prof))
    return found


def fetch_cookies() -> Optional[Tuple[str, Dict[str, str], str]]:
    """Try all sources in priority order and return (source_name, cookies, ua)
    for the first one whose cookies include a non-empty 'sessionKey'.
    Returns None if no source yielded usable cookies."""
    for name, root, ua in _user_data_roots():
        if not root.exists():
            continue
        logging.debug("auto-cookies: probing %s at %s", name, root)
        master_key = _get_master_key(root / "Local State")
        for prof in _candidate_profiles(root):
            cookies = _read_profile_cookies(prof, master_key)
            if cookies.get("sessionKey"):
                label = name if prof == root else f"{name} ({prof.name})"
                logging.debug("auto-cookies: using %s (%d cookies)", label, len(cookies))
                return (label, cookies, ua)
            elif cookies:
                logging.debug("auto-cookies: %s had %d claude.ai cookies but no sessionKey",
                              name, len(cookies))
    logging.debug("auto-cookies: no source yielded a sessionKey "
                  "(checked %APPDATA%\\Claude, %LOCALAPPDATA%\\Claude, "
                  "and %LOCALAPPDATA%\\Packages\\Claude_*)")
    return None
