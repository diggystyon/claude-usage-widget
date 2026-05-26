"""Unit tests for mac_cookie_sources.py crypto and detection helpers.

These tests run on any platform (Linux, Windows, macOS) because they
exercise pure-Python logic -- AES-CBC, PBKDF2-HMAC-SHA1, PKCS#7 padding,
and the sessionKey detection heuristic -- without touching macOS
Keychain, the security CLI, or any SQLite database. Real Keychain
interaction is covered by the post-build smoke test that runs on Mac CI
runners after the .app is built.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import mac_cookie_sources


# A 16-byte key we control. The real Chromium-on-Mac scheme derives this
# from a Keychain password via PBKDF2 (see _derive_key); for round-trip
# tests we just use a fixed value so we can encrypt ourselves and verify
# the decryption helper reverses it.
_TEST_KEY = b"0123456789abcdef"


# ---------- helpers ----------

def _encrypt_chromium_style(plaintext: bytes, key: bytes,
                            version_prefix: bytes = b"v10") -> bytes:
    """Encrypt `plaintext` using the Chromium-on-Mac scheme:
    AES-128-CBC, IV = 16 spaces (0x20), PKCS#7 padding, version prefix.
    Returns the bytes that would be stored in the `encrypted_value`
    column of the Cookies SQLite database.
    """
    if len(key) != 16:
        raise ValueError("key must be 16 bytes for AES-128")
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    enc = cipher.encryptor()
    body = enc.update(padded) + enc.finalize()
    return version_prefix + body


def _encrypt_raw_block(plaintext_block: bytes, key: bytes,
                       version_prefix: bytes = b"v10") -> bytes:
    """Encrypt a single 16-byte block AS-IS (no PKCS#7 padding added).
    Used to craft cases where the decrypted result has invalid padding."""
    assert len(plaintext_block) == 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    enc = cipher.encryptor()
    body = enc.update(plaintext_block) + enc.finalize()
    return version_prefix + body


# ---------- _decrypt_aes128_cbc: round-trip ----------

def test_roundtrip_v10_prefix():
    plaintext = b"sk-ant-sid01-this-looks-like-a-sessionkey"
    encrypted = _encrypt_chromium_style(plaintext, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out == plaintext.decode()


def test_roundtrip_v11_prefix():
    plaintext = b"another value"
    encrypted = _encrypt_chromium_style(plaintext, _TEST_KEY, b"v11")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out == plaintext.decode()


def test_roundtrip_block_aligned_plaintext():
    """A plaintext that's exactly 16 bytes triggers a full-block
    PKCS#7 padding (16 copies of 16). The strip must recognize that
    and produce the original plaintext, not silently trim a real byte."""
    plaintext = b"sixteen-bytes!!!"
    assert len(plaintext) == 16
    encrypted = _encrypt_chromium_style(plaintext, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out == plaintext.decode()


def test_roundtrip_long_plaintext():
    """Multi-block plaintext that exercises the CBC chain."""
    plaintext = (b"x" * 100) + b"|end"
    encrypted = _encrypt_chromium_style(plaintext, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out == plaintext.decode()


# ---------- _decrypt_aes128_cbc: reject malformed input ----------

def test_too_short_returns_none():
    assert mac_cookie_sources._decrypt_aes128_cbc(b"hi", _TEST_KEY) is None
    assert mac_cookie_sources._decrypt_aes128_cbc(b"", _TEST_KEY) is None


def test_empty_body_after_prefix_returns_none():
    assert mac_cookie_sources._decrypt_aes128_cbc(b"v10", _TEST_KEY) is None


def test_non_block_aligned_body_returns_none():
    # 17 bytes of body -- not a multiple of 16, so AES-CBC can't even
    # process it. The function must return None, not raise.
    encrypted = b"v10" + b"x" * 17
    assert mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY) is None


# ---------- _decrypt_aes128_cbc: strict PKCS#7 validation ----------
# These tests are the heart of the v1.2.0 PKCS#7 hardening change. Before
# the fix, the strip would happily trim N bytes whenever the last byte
# happened to be 1..16, even if the preceding bytes didn't match. That
# could silently produce garbage strings the API would later reject. The
# new strict check requires the trailing N bytes to all equal N.

def test_strict_padding_rejects_mismatched_trailing_bytes():
    """Decrypted result whose last byte is 5 but preceding 4 are not
    all 0x05 has invalid PKCS#7 padding and must be rejected."""
    # Build a 16-byte block where the last byte is 5 but the preceding
    # 4 bytes are NOT all 5 (so PKCS#7 padding would be invalid).
    block = b"xxxxxxxxxxxx\x01\x02\x03\x05"
    assert len(block) == 16 and block[-1] == 5 and block[-5:-1] != b"\x05\x05\x05\x05"
    encrypted = _encrypt_raw_block(block, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out is None


def test_strict_padding_accepts_valid_block():
    """Sanity check that the strict validation doesn't reject correctly-
    padded input. (The roundtrip tests above cover this too, but this
    one builds the block by hand to make the contract explicit.)"""
    # Plaintext "hi" + 14 copies of 14 = a valid PKCS#7-padded 16-byte block.
    block = b"hi" + bytes([14] * 14)
    assert len(block) == 16
    encrypted = _encrypt_raw_block(block, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out == "hi"


def test_strict_padding_rejects_pad_byte_zero():
    """A trailing byte of 0 is never valid PKCS#7 padding (pad length
    must be 1..block_size). Must reject."""
    block = b"yyyyyyyyyyyyyyy\x00"  # last byte = 0
    encrypted = _encrypt_raw_block(block, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out is None


def test_strict_padding_rejects_pad_byte_too_large():
    """A trailing byte of 17 would imply 17 bytes of padding, but our
    block is only 16. Must reject."""
    block = b"yyyyyyyyyyyyyyy\x11"  # last byte = 17 (0x11)
    encrypted = _encrypt_raw_block(block, _TEST_KEY, b"v10")
    out = mac_cookie_sources._decrypt_aes128_cbc(encrypted, _TEST_KEY)
    assert out is None


# ---------- _derive_key ----------

def test_derive_key_returns_16_bytes():
    """Chromium-on-Mac uses AES-128, so the derived key MUST be 16 bytes.
    If anyone changes the PBKDF2 length parameter this catches it."""
    k = mac_cookie_sources._derive_key(b"any-password")
    assert isinstance(k, bytes)
    assert len(k) == 16


def test_derive_key_deterministic():
    """Same password -> same key, every time. PBKDF2 is deterministic
    given fixed salt + iterations; we want to confirm we haven't
    accidentally introduced randomness."""
    k1 = mac_cookie_sources._derive_key(b"hunter2")
    k2 = mac_cookie_sources._derive_key(b"hunter2")
    assert k1 == k2


def test_derive_key_distinguishes_passwords():
    """Different passwords MUST produce different keys (basic KDF sanity)."""
    k_a = mac_cookie_sources._derive_key(b"password-a")
    k_b = mac_cookie_sources._derive_key(b"password-b")
    assert k_a != k_b


def test_derive_key_known_vector():
    """Regression test: pin the exact bytes for one known input so any
    accidental change to salt, iterations, hash, or length is caught.

    The Chromium-on-Mac scheme is:
        PBKDF2-HMAC-SHA1(password, salt=b'saltysalt', iterations=1003,
                          dklen=16).

    If someone bumps iterations to fix a security audit, this fails
    LOUDLY (which is what we want -- it changes the wire format and
    will silently make all existing cookies undecryptable).
    """
    # Computed once via the same library with the documented parameters:
    expected = bytes.fromhex("63009c1422826bb1e156c7a1a4f5b5a8")
    actual = mac_cookie_sources._derive_key(b"test")
    assert actual == expected, (
        f"PBKDF2 vector drifted! expected {expected.hex()} "
        f"got {actual.hex()}. Did salt or iteration count change?")


# ---------- sessionKey detection inside fetch_cookies ----------
# We can't easily run fetch_cookies() end-to-end without a real Mac
# environment (it touches the file system and the security CLI), but we
# can verify the detection predicate behaves correctly. The v1.2.0
# hardening added a >=20-char length requirement so a stale stub doesn't
# short-circuit the keychain decrypt path.

def _sessionkey_present(cookies: dict) -> bool:
    """Replicates the predicate used inside fetch_cookies() so we can
    test it in isolation. Kept in sync with the production code."""
    return any(
        (n.lower() == "sessionkey" or n.lower().endswith("sessionkey"))
        and isinstance(cookies.get(n), str)
        and len(cookies[n]) >= 20
        for n in cookies
    )


def test_sessionkey_detection_empty():
    assert _sessionkey_present({}) is False


def test_sessionkey_detection_short_value_rejected():
    """v1.1.3 would treat any sessionKey entry as present. v1.2.0 requires
    a plausible length so a stale stub doesn't bypass the keychain path."""
    assert _sessionkey_present({"sessionKey": "x"}) is False
    assert _sessionkey_present({"sessionKey": ""}) is False
    # 19 chars: still below the threshold.
    assert _sessionkey_present({"sessionKey": "x" * 19}) is False


def test_sessionkey_detection_plausible_value_accepted():
    assert _sessionkey_present({"sessionKey": "x" * 20}) is True
    # Real-world sessionKeys are several hundred chars.
    assert _sessionkey_present(
        {"sessionKey": "sk-ant-sid01-" + ("a" * 200)}) is True


def test_sessionkey_detection_wrong_name_rejected():
    assert _sessionkey_present({"foo": "x" * 50}) is False


def test_sessionkey_detection_case_insensitive():
    assert _sessionkey_present({"SessionKey": "x" * 30}) is True
    assert _sessionkey_present({"SESSIONKEY": "x" * 30}) is True


def test_sessionkey_detection_suffix_match():
    """The predicate also matches names that END with 'sessionkey' --
    a convention some Chromium builds use for prefixed cookies."""
    assert _sessionkey_present({"__Host-sessionkey": "x" * 30}) is True
