"""
mac_cookie_sources.py
---------------------
Read claude.ai cookies from the Claude desktop app on macOS.

Electron stores cookies in a standard Chromium SQLite layout inside the app's
userData directory:
  ~/Library/Application Support/Claude/Cookies               (older Electron)
  ~/Library/Application Support/Claude/Network/Cookies       (newer Electron)
  ~/Library/Application Support/Claude/Default/Network/Cookies (rare)

Cookie values may be plaintext OR AES-128-CBC encrypted with a v10/v11 prefix
(standard Chromium-on-macOS scheme). The decryption key lives in macOS
Keychain under a generic-password entry named "<AppName> Safe Storage" -- in
our case "Claude Safe Storage". We fetch it via `security find-generic-password`,
which prompts the user once for keychain access; subsequent fetches are silent
after the user clicks "Always Allow".

We intentionally do NOT touch Safari, Chrome, or Edge cookies. The Claude
desktop app being signed-in is the supported (and simplest) requirement.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Electron uses a Chrome User-Agent by default; matching it keeps the API
# happy and avoids 403s from any UA-sniffing layer at claude.ai.
UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Names of Keychain entries we'll try, in order. Electron apps usually set
# "<AppName> Safe Storage" but some older variants used "Electron Safe Storage".
_KEYCHAIN_NAMES = (
    "Claude Safe Storage",
    "Claude",
    "Electron Safe Storage",
)


def _userdata_root() -> Optional[Path]:
    """Return the Claude.app userData directory if it exists.

    On macOS, Electron apps put per-app state under
    ~/Library/Application Support/<AppName>/. For Claude.app that's just
    ~/Library/Application Support/Claude/. We verify it looks like a real
    Chromium profile by checking for either a Cookies file or a Local State
    file (which Chromium writes on first run).
    """
    home = Path.home()
    root = home / "Library" / "Application Support" / "Claude"
    if not root.is_dir():
        return None
    looks_real = (
        (root / "Local State").exists()
        or (root / "Cookies").exists()
        or (root / "Network" / "Cookies").exists()
        or (root / "Default" / "Cookies").exists()
        or (root / "Default" / "Network" / "Cookies").exists()
    )
    return root if looks_real else None


def _find_cookies_db(root: Path) -> Optional[Path]:
    """Return the path to the most likely Cookies SQLite file inside root."""
    candidates = [
        root / "Network" / "Cookies",
        root / "Cookies",
        root / "Default" / "Network" / "Cookies",
        root / "Default" / "Cookies",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _keychain_password() -> Optional[bytes]:
    """Fetch the Claude Safe Storage password from macOS Keychain.

    Calls /usr/bin/security with -w (print raw password). The first invocation
    pops a system dialog: "security wants to access key 'Claude Safe Storage'
    in your keychain." The user picks Always Allow / Allow / Deny. After
    Always Allow, every subsequent call is silent.

    Returns the password as bytes, or None on failure / denial.
    """
    for name in _KEYCHAIN_NAMES:
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-w", "-s", name],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logging.debug("keychain lookup '%s' failed: %s", name, e)
            continue
        if r.returncode == 0:
            pw = r.stdout.strip()
            if pw:
                logging.info("keychain: found '%s'", name)
                return pw.encode("utf-8")
        else:
            logging.debug("keychain lookup '%s' rc=%s stderr=%s",
                          name, r.returncode, r.stderr.strip()[:200])
    return None


def _derive_key(password: bytes) -> bytes:
    """Chromium-on-macOS key derivation: PBKDF2-HMAC-SHA1, salt='saltysalt',
    1003 iterations, 16-byte key. Identical across all Chromium-based
    Electron apps on Mac."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
    )
    return kdf.derive(password)


def _decrypt_aes128_cbc(ciphertext: bytes, key: bytes) -> Optional[str]:
    """Decrypt a single encrypted_value using the Chromium scheme:
    AES-128-CBC, IV = 16 spaces (0x20). The first 3 bytes ("v10" or "v11")
    are a version tag; strip them before decrypting.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(ciphertext) < 3:
        return None
    # Skip the version prefix ("v10" or "v11").
    body = ciphertext[3:]
    if len(body) == 0 or len(body) % 16 != 0:
        return None
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
        dec = cipher.decryptor()
        plain = dec.update(body) + dec.finalize()
        # PKCS#7 padding strip.
        if not plain:
            return None
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
        return plain.decode("utf-8", errors="replace")
    except Exception:
        logging.exception("decrypt failed")
        return None


def _read_cookies_db(db_path: Path, key: Optional[bytes]) -> Dict[str, str]:
    """Read claude.ai cookies from the SQLite store. Decrypt any
    encrypted_value blobs using the given key (if supplied).

    Returns name -> value. Cookies whose value is empty AND have no decryption
    key are skipped. Cookies whose host_key doesn't end with .claude.ai are
    skipped.
    """
    # Copy to a tempfile so we don't trip over any locks if Claude.app
    # happens to be writing to the DB. macOS is much friendlier than Windows
    # here (no exclusive locks) but a snapshot is still safer.
    out: Dict[str, str] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "Cookies.sqlite"
        try:
            tmp_db.write_bytes(db_path.read_bytes())
        except Exception:
            logging.exception("copy %s -> tempfile failed", db_path)
            return out
        try:
            con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True, timeout=5.0)
        except Exception:
            logging.exception("sqlite open failed: %s", tmp_db)
            return out
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT host_key, name, value, encrypted_value "
                "FROM cookies "
                "WHERE host_key LIKE '%claude.ai%'"
            )
            for host, name, plain_val, enc_val in cur.fetchall():
                if not name:
                    continue
                val: Optional[str] = None
                if plain_val:
                    val = plain_val
                elif enc_val and key is not None:
                    val = _decrypt_aes128_cbc(bytes(enc_val), key)
                if val:
                    out[name] = val
        except Exception:
            logging.exception("cookie query failed")
        finally:
            con.close()
    return out


def fetch_cookies() -> Optional[Tuple[str, Dict[str, str], str]]:
    """Discover Claude.app's userData, fetch its keychain key, and read every
    claude.ai cookie.

    Returns (source_label, cookies_dict, user_agent) on success, None on any
    failure. The widget's TrayApp / MenuBarApp treats None as 'fall back to
    paste-mode cookies'.
    """
    root = _userdata_root()
    if root is None:
        logging.debug("Claude.app userData not found")
        return None
    db = _find_cookies_db(root)
    if db is None:
        logging.debug("Cookies SQLite not found inside %s", root)
        return None

    # First try without the key -- many cookies are stored plaintext on
    # macOS (only the sensitive ones like sessionKey are encrypted).
    cookies = _read_cookies_db(db, key=None)
    have_session = any(n.lower().endswith("sessionkey") or n.lower() == "sessionkey"
                       for n in cookies)
    if not have_session:
        # Need the keychain password to decrypt sessionKey.
        pw = _keychain_password()
        if pw is None:
            logging.info("Keychain access denied/missing; cookies incomplete")
            return None
        key = _derive_key(pw)
        cookies = _read_cookies_db(db, key=key)
    if not cookies:
        return None
    return ("Claude desktop app", cookies, UA_DESKTOP)


def status() -> Dict[str, object]:
    """Lightweight diagnostic used by the menu's debug action.
    Returns a dict describing what we found without leaking cookie values."""
    info: Dict[str, object] = {
        "userdata_root": None,
        "cookies_db": None,
        "cookie_count": 0,
        "has_sessionkey": False,
        "keychain_available": False,
    }
    root = _userdata_root()
    if root is not None:
        info["userdata_root"] = str(root)
    db = _find_cookies_db(root) if root else None
    if db is not None:
        info["cookies_db"] = str(db)
    pw = _keychain_password()
    if pw:
        info["keychain_available"] = True
    if root and db:
        cookies = _read_cookies_db(db, _derive_key(pw) if pw else None)
        info["cookie_count"] = len(cookies)
        info["has_sessionkey"] = any(
            n.lower() == "sessionkey" for n in cookies
        )
    return info
