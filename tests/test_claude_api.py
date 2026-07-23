"""Unit tests for claude_api.py: the shared usage-API + status + version
helpers used by both entry scripts.

These tests exercise pure-Python parsing / extraction logic. They do NOT
hit the network -- httpx clients are mocked or simply not constructed.
"""
from __future__ import annotations

import os

import claude_api


# ---------- _parse_version ----------

def test_parse_version_simple():
    assert claude_api._parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_strips_v_prefix():
    assert claude_api._parse_version("v1.2.3") == (1, 2, 3)
    assert claude_api._parse_version("V1.2.3") == (1, 2, 3)


def test_parse_version_handles_empty():
    assert claude_api._parse_version("") == (0,)
    assert claude_api._parse_version(None) == (0,)  # type: ignore[arg-type]


def test_parse_version_handles_garbage_segments():
    # Non-numeric pieces become 0 -- defensive against weird tag schemes.
    assert claude_api._parse_version("1.beta.3") == (1, 0, 3)


def test_parse_version_ordering():
    # The whole point of having this function: comparing versions for the
    # "update available" toast.
    assert claude_api._parse_version("1.3.0") > claude_api._parse_version("1.2.9")
    assert claude_api._parse_version("v2.0.0") > claude_api._parse_version("1.99.99")
    assert claude_api._parse_version("1.2.0") == claude_api._parse_version("1.2.0")


# ---------- classify_display_state (v1.3.5) ----------

def test_display_state_unconfigured_when_never_succeeded():
    # Fresh install: no successful read yet -> setup-needed state, even if
    # session_pct happens to be 0. This is the fix for "0%/1% looked like
    # real data" -- the widget must not present absent data as live.
    assert claude_api.classify_display_state(False, 0) == "unconfigured"


def test_display_state_unconfigured_beats_stale():
    # Priority: a brand-new install that is ALSO failing auth should read
    # as 'set me up', not 'your data went stale'. unconfigured wins.
    assert claude_api.classify_display_state(False, 99) == "unconfigured"


def test_display_state_live_when_succeeded_and_no_failures():
    assert claude_api.classify_display_state(True, 0) == "live"


def test_display_state_live_below_stale_threshold():
    # One or two transient auth blips (e.g. a session rotation) must NOT
    # flip the UI into an alarm state. Only sticky failure is "stale".
    assert claude_api.classify_display_state(True, 1) == "live"
    assert claude_api.classify_display_state(True, 2) == "live"


def test_display_state_stale_at_threshold():
    assert claude_api.classify_display_state(True, 3) == "stale"
    assert claude_api.classify_display_state(True, 10) == "stale"


def test_display_state_respects_custom_threshold():
    assert claude_api.classify_display_state(True, 2, stale_threshold=2) == "stale"
    assert claude_api.classify_display_state(True, 1, stale_threshold=2) == "live"


# ---------- backfill_ever_succeeded config migration (v1.3.6) ----------

def test_backfill_sets_true_for_upgrading_working_user():
    # Config from <1.3.5 (no ever_succeeded key) but with a real source:
    # the user was already working, so they must NOT see "setup needed"
    # flash on first launch after the upgrade.
    cfg = {"last_source": "Claude browser extension"}
    out = claude_api.backfill_ever_succeeded(cfg)
    assert out["ever_succeeded"] is True
    assert out is cfg  # mutates in place


def test_backfill_leaves_brand_new_install_unconfigured():
    # Fresh install: no source recorded yet -> stays unconfigured so the
    # setup hint shows.
    cfg = {"last_source": ""}
    claude_api.backfill_ever_succeeded(cfg)
    assert cfg.get("ever_succeeded") in (None, False)


def test_backfill_does_not_clobber_existing_true():
    cfg = {"last_source": "Manual paste", "ever_succeeded": True}
    claude_api.backfill_ever_succeeded(cfg)
    assert cfg["ever_succeeded"] is True


def test_backfill_does_not_resurrect_explicit_false_without_source():
    # An already-migrated config that legitimately never succeeded must
    # stay False.
    cfg = {"last_source": "", "ever_succeeded": False}
    claude_api.backfill_ever_succeeded(cfg)
    assert cfg["ever_succeeded"] is False


# ---------- ClaudeUsageFetcher._extract_percents ----------

def test_extract_percents_five_hour_seven_day_shape():
    """The most common API response shape: five_hour + seven_day keys
    with utilization + resets_at."""
    data = {
        "five_hour":  {"utilization": 42.5, "resets_at": "2026-05-26T14:00:00Z"},
        "seven_day":  {"utilization": 12.0, "resets_at": "2026-06-01T00:00:00Z"},
    }
    out = claude_api.ClaudeUsageFetcher._extract_percents(data)
    assert out == (42.5, 12.0, "2026-05-26T14:00:00Z", "2026-06-01T00:00:00Z")


def test_extract_percents_alt_session_keys():
    """The API has shipped several shapes; we accept any of the keys
    in SESSION_KEYS / WEEKLY_KEYS."""
    data = {
        "current_session": {"utilization": 50},
        "weekly":          {"utilization": 25},
    }
    out = claude_api.ClaudeUsageFetcher._extract_percents(data)
    assert out is not None
    s, w, _, _ = out
    assert s == 50.0 and w == 25.0


def test_extract_percents_missing_resets_at_is_empty_string():
    data = {
        "five_hour":  {"utilization": 10},
        "seven_day":  {"utilization": 20},
    }
    out = claude_api.ClaudeUsageFetcher._extract_percents(data)
    assert out == (10.0, 20.0, "", "")


def test_extract_percents_rejects_out_of_range():
    """Percentages outside [0, 100] are rejected as 'no recognizable
    value' -- defensive against the API ever shipping a fraction (0.42)
    that we'd otherwise misinterpret as 0.42% -- though we intentionally
    DO accept whole-number 0-100 (so 1.0 means 1%, not 100%)."""
    data = {
        "five_hour":  {"utilization": 150.0},  # over 100 -> reject
        "seven_day":  {"utilization": 20.0},
    }
    out = claude_api.ClaudeUsageFetcher._extract_percents(data)
    assert out is None  # missing session_pct -> None


def test_extract_percents_handles_string_numbers():
    """The API sometimes returns numbers as strings; float() handles
    that. Defensive coverage."""
    data = {
        "five_hour":  {"utilization": "33.5"},
        "seven_day":  {"utilization": "7"},
    }
    out = claude_api.ClaudeUsageFetcher._extract_percents(data)
    assert out is not None
    assert out[0] == 33.5 and out[1] == 7.0


def test_extract_percents_returns_none_for_empty():
    assert claude_api.ClaudeUsageFetcher._extract_percents({}) is None
    assert claude_api.ClaudeUsageFetcher._extract_percents(None) is None
    assert claude_api.ClaudeUsageFetcher._extract_percents("not a dict") is None


def test_extract_percents_returns_none_for_partial():
    # session only -- missing weekly should fail the whole extraction.
    data = {"five_hour": {"utilization": 50}}
    assert claude_api.ClaudeUsageFetcher._extract_percents(data) is None


# ---------- ClaudeUsageFetcher constructor ----------

def test_fetcher_requires_user_agent():
    """The shared module forces callers to pass a UA -- the old
    DEFAULT_UA fallback was the source of platform-drift bugs where
    Mac would silently send a Windows UA if the cookie source didn't
    supply one. Now both platforms must be explicit."""
    import pytest
    with pytest.raises(ValueError):
        claude_api.ClaudeUsageFetcher({"sessionKey": "x"}, user_agent="")
    with pytest.raises(ValueError):
        # type: ignore[arg-type]
        claude_api.ClaudeUsageFetcher({"sessionKey": "x"}, user_agent=None)  # type: ignore[arg-type]


def test_fetcher_sets_platform_hint():
    """The sec-ch-ua-platform hint must reflect the caller's claim.
    claude.ai uses this header for browser fingerprinting / A-B routing;
    sending Windows from a Mac (or vice versa) is the kind of mismatch
    that could trigger unusual error responses."""
    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"}, user_agent="test-ua", platform="macOS")
    try:
        assert f.client.headers.get("sec-ch-ua-platform") == '"macOS"'
        assert f.client.headers.get("User-Agent") == "test-ua"
    finally:
        f.close()

    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"}, user_agent="other-ua", platform="Windows")
    try:
        assert f.client.headers.get("sec-ch-ua-platform") == '"Windows"'
    finally:
        f.close()


def test_fetcher_extra_headers_filtered():
    """The cURL-paste path forwards arbitrary headers, but Cookie /
    Host / Content-Length must NEVER be forwarded -- httpx manages them
    itself and external values would conflict."""
    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"},
        user_agent="test-ua",
        extra_headers={
            "Cookie": "should-be-stripped",
            "Host":   "evil.example.com",
            "Content-Length": "9999",
            "X-Custom": "should-be-kept",
        },
    )
    try:
        # X-Custom must survive.
        assert f.client.headers.get("X-Custom") == "should-be-kept"
        # The dangerous ones must NOT be in headers (httpx adds Cookie/Host
        # itself based on the cookies dict + request URL).
        assert f.client.headers.get("Host") != "evil.example.com"
        # Cookie is managed via the cookies arg, not headers; if it leaked,
        # httpx might double-set it.
        cookie_hdr = f.client.headers.get("Cookie", "")
        assert "should-be-stripped" not in cookie_hdr
    finally:
        f.close()


# ---------- constants ----------

def test_status_colors_has_operational_with_no_dot():
    label, color = claude_api.STATUS_COLORS["operational"]
    assert label == "All systems operational"
    assert color is None  # no overlay dot when everything's fine


def test_status_colors_outages_have_colors():
    for key in ("degraded_performance", "partial_outage",
                "major_outage", "under_maintenance"):
        label, color = claude_api.STATUS_COLORS[key]
        assert label  # non-empty
        assert color is not None and len(color) == 3


# ---------- ClaudeUsageFetcher header stripping (extended) ----------

def test_fetcher_strips_authorization_and_friends():
    """v1.3.1: extra_headers filter was extended to strip not just
    cookie/host/content-length but also authorization, origin, referer,
    accept-encoding. If any of those leak through, the request either
    decodes wrong (br with no brotli installed) or impersonates a
    different security context."""
    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"},
        user_agent="test-ua",
        extra_headers={
            "Authorization": "Bearer attacker-token",
            "Origin": "https://evil.example.com",
            "Referer": "https://evil.example.com/",
            "Accept-Encoding": "br",
            "X-Real-Custom": "should-survive",
        },
    )
    try:
        h = f.client.headers
        # These would override our claude.ai-correct values or break decode.
        assert h.get("Authorization") != "Bearer attacker-token"
        assert h.get("Origin") == "https://claude.ai"
        assert h.get("Referer") == "https://claude.ai/settings/billing"
        # Accept-Encoding either absent or httpx default (NOT br alone).
        ae = (h.get("Accept-Encoding") or "").lower()
        assert "br" not in ae or ae != "br"  # br can appear in httpx default but never as the only value from our filter
        # Genuine custom header passes through.
        assert h.get("X-Real-Custom") == "should-survive"
    finally:
        f.close()


# ---------- fetch_claude_status SSL cascade ----------
#
# These tests patch claude_api.httpx.Client to verify the cascade order
# is: (1) default -> (2) certifi-explicit -> (3) verify=False.
# Pre-v1.2.0 Mac had the cascade at the constructor level, where it was
# unreachable -- httpx.Client() never raises on its own. Verifying the
# cascade now lives at the request site protects against a future
# "cleanup" silently re-introducing that bug.

class _MockResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "components": [{"name": "claude.ai", "status": "operational"}]
        }
        self.text = "ok"

    def json(self):
        return self._payload


def _mock_client_factory(*, raise_for, returns):
    """Build a fake httpx.Client class that records each construction
    and raises/returns according to the script.

    raise_for: list of (verify_arg, exception_or_None) -- in order; for
      each construction we raise the matching exception if not None, or
      yield the matching mock response from `returns` if None.
    returns: list of _MockResp to yield in order on successful .get().
    """
    instances = {"count": 0, "kwargs": []}
    returns_iter = iter(returns)

    class _MC:
        def __init__(self, *args, **kwargs):
            i = instances["count"]
            instances["count"] += 1
            instances["kwargs"].append(kwargs)
            self._script = raise_for[i] if i < len(raise_for) else None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):  # accept headers= and similar
            if self._script is not None:
                raise self._script
            return next(returns_iter)

    return _MC, instances


def test_status_cascade_default_succeeds(monkeypatch):
    """Happy path: the default SSL strategy works, no fallback attempted."""
    import httpx as _httpx
    MC, instances = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out == ("operational", "All systems operational")
    # Exactly ONE client constructed (no fallback needed).
    assert instances["count"] == 1
    # Default strategy has no `verify` kwarg.
    assert "verify" not in instances["kwargs"][0]


def test_status_cascade_falls_through_default_to_certifi(monkeypatch, tmp_path):
    """Default SSL fails with a transport error -> we fall through to
    the certifi-explicit strategy, which succeeds."""
    import httpx as _httpx
    # Force certifi.where() to return a path we know exists, so the
    # certifi strategy actually gets added.
    fake_pem = tmp_path / "cacert.pem"
    fake_pem.write_text("dummy")
    monkeypatch.setattr(claude_api, "_status_ssl_strategies",
                        lambda: [("default", None), ("certifi", str(fake_pem)), ("unverified", False)])

    MC, instances = _mock_client_factory(
        raise_for=[_httpx.ConnectError("ssl"), None],
        returns=[_MockResp(200)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out == ("operational", "All systems operational")
    # Two clients constructed: default (failed) + certifi (succeeded).
    assert instances["count"] == 2
    assert instances["kwargs"][0].get("verify") is None or "verify" not in instances["kwargs"][0]
    assert instances["kwargs"][1].get("verify") == str(fake_pem)


def test_status_cascade_falls_through_to_unverified(monkeypatch):
    """Both default AND certifi fail -> we use verify=False as a last
    resort. This is the path that keeps the status dot working when the
    bundled CA path is broken inside a PyInstaller bundle."""
    import httpx as _httpx
    monkeypatch.setattr(claude_api, "_status_ssl_strategies",
                        lambda: [("default", None), ("certifi", "/no/such"), ("unverified", False)])
    MC, instances = _mock_client_factory(
        raise_for=[
            _httpx.ConnectError("ssl1"),
            _httpx.ConnectError("ssl2"),
            None,
        ],
        returns=[_MockResp(200)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out == ("operational", "All systems operational")
    assert instances["count"] == 3
    assert instances["kwargs"][2].get("verify") is False


def test_status_cascade_all_strategies_fail_returns_none(monkeypatch):
    """Every strategy fails -> return None (status dot stays whatever
    it was, doesn't crash the loop)."""
    import httpx as _httpx
    monkeypatch.setattr(claude_api, "_status_ssl_strategies",
                        lambda: [("default", None), ("unverified", False)])
    MC, instances = _mock_client_factory(
        raise_for=[
            _httpx.ConnectError("a"),
            _httpx.ConnectError("b"),
        ],
        returns=[],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out is None
    assert instances["count"] == 2


def test_status_non_200_returns_none_without_fallback(monkeypatch):
    """A real HTTP response with status != 200 is not an SSL failure --
    we treat it as 'no signal' and return None without trying other
    strategies. Statuspage being briefly 503 shouldn't burn the
    fallback cascade."""
    MC, instances = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(503)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out is None
    assert instances["count"] == 1


def test_status_parses_non_operational_state(monkeypatch):
    """When claude.ai reports degraded_performance, we surface the
    right label."""
    payload = {
        "components": [
            {"name": "API", "status": "operational"},
            {"name": "Claude.ai", "status": "degraded_performance"},
        ],
    }
    MC, _ = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200, payload=payload)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    out = claude_api.fetch_claude_status()
    assert out == ("degraded_performance", "Degraded performance")


# ---------- fetch_latest_version ----------

def test_fetch_latest_version_happy_path(monkeypatch):
    payload = {"tag_name": "v1.5.0", "draft": False, "prerelease": False}
    MC, _ = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200, payload=payload)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    assert claude_api.fetch_latest_version("test-ua/1.0") == "1.5.0"


def test_fetch_latest_version_strips_v_prefix(monkeypatch):
    """Both 'v1.5.0' and '1.5.0' tags are valid; we always strip the
    leading 'v' for comparison."""
    payload = {"tag_name": "v2.0.0"}
    MC, _ = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200, payload=payload)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    assert claude_api.fetch_latest_version("test-ua/1.0") == "2.0.0"


def test_fetch_latest_version_skips_draft(monkeypatch):
    """Draft releases must never be surfaced as 'update available' --
    they're invisible to the public anyway."""
    payload = {"tag_name": "v9.9.9", "draft": True}
    MC, _ = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200, payload=payload)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    assert claude_api.fetch_latest_version("test-ua/1.0") is None


def test_fetch_latest_version_skips_prerelease(monkeypatch):
    """Same for prereleases -- the GitHub /releases/latest endpoint
    already filters these, but we defense-in-depth on the client too."""
    payload = {"tag_name": "v9.9.9-rc1", "prerelease": True}
    MC, _ = _mock_client_factory(
        raise_for=[None],
        returns=[_MockResp(200, payload=payload)],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    assert claude_api.fetch_latest_version("test-ua/1.0") is None


def test_fetch_latest_version_non_200_returns_none(monkeypatch):
    """Rate-limited (403), gone (404), or server error: all return
    None so the loop just sleeps and tries again later."""
    for code in (403, 404, 500, 502):
        MC, _ = _mock_client_factory(
            raise_for=[None],
            returns=[_MockResp(code)],
        )
        monkeypatch.setattr(claude_api.httpx, "Client", MC)
        assert claude_api.fetch_latest_version("test-ua/1.0") is None, f"code={code}"


def test_fetch_latest_version_network_error_returns_none(monkeypatch):
    """Network down? Return None so the update loop just retries."""
    import httpx as _httpx
    MC, _ = _mock_client_factory(
        raise_for=[_httpx.ConnectError("no network")],
        returns=[],
    )
    monkeypatch.setattr(claude_api.httpx, "Client", MC)
    assert claude_api.fetch_latest_version("test-ua/1.0") is None


def test_fetch_latest_version_passes_user_agent(monkeypatch):
    """GitHub requires a User-Agent on API calls. Verify ours actually
    goes through."""
    payload = {"tag_name": "v1.0.0"}
    seen_headers = {}

    class _MC:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            seen_headers.update(headers or {})

            class R:
                status_code = 200

                def json(self_inner):
                    return payload

            return R()

    monkeypatch.setattr(claude_api.httpx, "Client", _MC)
    claude_api.fetch_latest_version("my-app/1.2.3")
    assert seen_headers.get("User-Agent") == "my-app/1.2.3"


# ---------- CA bundle persistence (mid-run temp-cleaner hardening) ----------
#
# Regression coverage for the recurring failure where PyInstaller's
# extracted cacert.pem is deleted from the _MEI temp dir by a disk/temp
# cleaner while the widget runs, breaking all TLS. The fix relocates the
# bundle to a stable, widget-owned dir and re-resolves if it ever goes
# missing. See _persist_ca_bundle / _setup_bundled_certifi / _ensure_ca_bundle.


def test_stable_ca_dir_windows_branch(monkeypatch, tmp_path):
    """Non-mac uses %APPDATA%\\ClaudeUsageTray (mirrors the entry scripts'
    CONFIG_DIR so the bundle sits next to config.json)."""
    monkeypatch.setattr(claude_api.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert claude_api._stable_ca_dir() == os.path.join(str(tmp_path), "ClaudeUsageTray")


def test_stable_ca_dir_mac_branch(monkeypatch):
    """macOS uses ~/Library/Application Support/ClaudeUsageTray."""
    monkeypatch.setattr(claude_api.sys, "platform", "darwin")
    d = claude_api._stable_ca_dir()
    assert d.endswith(os.path.join("Library", "Application Support", "ClaudeUsageTray"))


def test_persist_ca_bundle_copies_to_stable_dir(monkeypatch, tmp_path):
    """A resolved temp bundle is copied into the stable dir; the returned
    path is the stable copy and it has the source's contents."""
    src = tmp_path / "src" / "cacert.pem"
    src.parent.mkdir()
    src.write_text("CERTDATA")
    stable = tmp_path / "stable"
    monkeypatch.setattr(claude_api, "_stable_ca_dir", lambda: str(stable))
    dest = claude_api._persist_ca_bundle(str(src))
    assert dest == os.path.join(str(stable), "cacert.pem")
    assert os.path.isfile(dest)
    with open(dest) as f:
        assert f.read() == "CERTDATA"


def test_persist_ca_bundle_noop_when_src_is_dest(monkeypatch, tmp_path):
    """If the source already IS the stable copy, return it without copying
    onto itself."""
    stable = tmp_path / "stable"
    stable.mkdir()
    dest = stable / "cacert.pem"
    dest.write_text("X")
    monkeypatch.setattr(claude_api, "_stable_ca_dir", lambda: str(stable))
    got = claude_api._persist_ca_bundle(str(dest))
    assert got == os.path.join(str(stable), "cacert.pem")
    assert dest.read_text() == "X"


def test_persist_ca_bundle_skips_recopy_when_same_size(monkeypatch, tmp_path):
    """An existing stable copy of the same size is treated as current and
    left untouched (cheap once-per-launch check, no needless write)."""
    src = tmp_path / "cacert.pem"
    src.write_text("AAAA")  # 4 bytes
    stable = tmp_path / "stable"
    stable.mkdir()
    dest = stable / "cacert.pem"
    dest.write_text("BBBB")  # same size, different content
    monkeypatch.setattr(claude_api, "_stable_ca_dir", lambda: str(stable))
    claude_api._persist_ca_bundle(str(src))
    assert dest.read_text() == "BBBB"


def test_persist_ca_bundle_recopies_when_size_differs(monkeypatch, tmp_path):
    """A new release ships a different-size bundle -> refresh the stable
    copy."""
    src = tmp_path / "cacert.pem"
    src.write_text("AAAAAAAA")  # 8 bytes
    stable = tmp_path / "stable"
    stable.mkdir()
    dest = stable / "cacert.pem"
    dest.write_text("BBBB")  # 4 bytes
    monkeypatch.setattr(claude_api, "_stable_ca_dir", lambda: str(stable))
    claude_api._persist_ca_bundle(str(src))
    assert dest.read_text() == "AAAAAAAA"


def test_persist_ca_bundle_returns_src_on_failure(monkeypatch, tmp_path):
    """If the copy can't be made, fall back to the original path -- a temp
    cert is better than none."""
    src = tmp_path / "cacert.pem"
    src.write_text("X")
    monkeypatch.setattr(claude_api, "_stable_ca_dir", lambda: str(tmp_path / "stable"))

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(claude_api.os, "makedirs", boom)
    assert claude_api._persist_ca_bundle(str(src)) == str(src)


def test_setup_certifi_persists_when_frozen(monkeypatch, tmp_path):
    """In a frozen bundle, the resolved cert is routed through
    _persist_ca_bundle and SSL_CERT_FILE points at the result."""
    import certifi
    fake = tmp_path / "cacert.pem"
    fake.write_text("X")
    monkeypatch.setattr(certifi, "where", lambda: str(fake))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(claude_api.sys, "frozen", True, raising=False)
    calls = []

    def spy(src):
        calls.append(src)
        return src

    monkeypatch.setattr(claude_api, "_persist_ca_bundle", spy)
    claude_api._setup_bundled_certifi()
    assert calls == [os.path.normpath(str(fake))]
    assert os.environ["SSL_CERT_FILE"] == os.path.normpath(str(fake))
    assert os.environ["REQUESTS_CA_BUNDLE"] == os.path.normpath(str(fake))


def test_setup_certifi_does_not_persist_when_not_frozen(monkeypatch, tmp_path):
    """In dev/test (not frozen) the site-packages bundle is used directly;
    we must not write into %APPDATA%."""
    import certifi
    fake = tmp_path / "cacert.pem"
    fake.write_text("X")
    monkeypatch.setattr(certifi, "where", lambda: str(fake))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(claude_api.sys, "frozen", False, raising=False)
    calls = []
    monkeypatch.setattr(claude_api, "_persist_ca_bundle", lambda s: calls.append(s))
    claude_api._setup_bundled_certifi()
    assert calls == []
    assert os.environ["SSL_CERT_FILE"] == os.path.normpath(str(fake))


def test_ensure_ca_bundle_noop_when_not_frozen(monkeypatch):
    """Guard is inert outside a frozen bundle -- no re-resolution attempts
    during ordinary dev/test runs."""
    monkeypatch.setattr(claude_api.sys, "frozen", False, raising=False)
    called = []
    monkeypatch.setattr(claude_api, "_setup_bundled_certifi", lambda: called.append(1))
    claude_api._ensure_ca_bundle()
    assert called == []


def test_ensure_ca_bundle_reresolves_when_frozen_and_missing(monkeypatch, tmp_path):
    """Frozen + SSL_CERT_FILE points at a now-deleted file -> re-run
    resolution instead of letting httpx crash on it."""
    monkeypatch.setattr(claude_api.sys, "frozen", True, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    called = []
    monkeypatch.setattr(claude_api, "_setup_bundled_certifi", lambda: called.append(1))
    claude_api._ensure_ca_bundle()
    assert called == [1]


def test_ensure_ca_bundle_noop_when_frozen_and_present(monkeypatch, tmp_path):
    """Frozen + the bundle still exists -> cheap early return, no work."""
    p = tmp_path / "cacert.pem"
    p.write_text("X")
    monkeypatch.setattr(claude_api.sys, "frozen", True, raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(p))
    called = []
    monkeypatch.setattr(claude_api, "_setup_bundled_certifi", lambda: called.append(1))
    claude_api._ensure_ca_bundle()
    assert called == []


# ---------- error_code parsing on non-200 (v1.3.4) ----------

def test_parse_error_code_standard_account_session_invalid():
    """The widget keys its 'sign in to claude.ai' tooltip off this exact
    error_code. If claude.ai ever changes the field name we want the tests
    to scream, not to silently keep showing 'Refresh failed' forever."""
    body = (
        '{"type":"error",'
        '"error":{"type":"permission_error",'
        '"message":"Invalid authorization",'
        '"details":{"error_visibility":"user_facing",'
        '"error_code":"account_session_invalid"}},'
        '"request_id":"req_011Cb"}'
    )
    assert claude_api.ClaudeUsageFetcher._parse_error_code(body) \
        == "account_session_invalid"


def test_parse_error_code_returns_none_on_garbage():
    """Anything that isn't a parseable error envelope returns None.
    Callers treat None as 'unknown failure', so a false-positive here
    would mislabel a transport blip as an auth failure."""
    for bad in ["", "not json at all", "{}", '{"error":"string"}',
                '{"error":{"details":null}}',
                '{"error":{"details":{"error_code":42}}}',  # not a string
                '{"error":{"details":{"error_code":""}}}']:
        assert claude_api.ClaudeUsageFetcher._parse_error_code(bad) is None, bad


def test_parse_error_code_handles_alternative_codes():
    """The widget only special-cases account_session_invalid today, but
    the parser should faithfully return whatever code claude.ai sent so
    future code can branch on more cases without re-parsing."""
    body = (
        '{"error":{"details":{"error_code":"organization_not_found"}}}'
    )
    assert claude_api.ClaudeUsageFetcher._parse_error_code(body) \
        == "organization_not_found"


def test_fetch_sets_last_error_code_on_403(monkeypatch):
    """The widget's auth-failure counter reads fetcher.last_error_code
    after a None result. This test wires up a fake claude.ai that returns
    the real-world account_session_invalid body on /usage and verifies
    fetch() exposes both the status code and the parsed error_code."""
    body = (
        '{"type":"error","error":{"type":"permission_error",'
        '"message":"Invalid authorization",'
        '"details":{"error_code":"account_session_invalid"}}}'
    )

    class _R:
        status_code = 403
        text = body

        def json(self_inner):
            import json
            return json.loads(body)

    class _MC:
        def __init__(self, **kwargs):
            self.headers = {}
            self.cookies = {}

        def get(self, url, **kwargs):
            return _R()

        def close(self):
            pass

    monkeypatch.setattr(claude_api.httpx, "Client", _MC)
    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"}, user_agent="ua",
        # org_id set so fetch() doesn't try to discover one first.
        org_id="00000000-0000-0000-0000-000000000000",
    )
    try:
        assert f.fetch() is None
        assert f.last_status_code == 403
        assert f.last_error_code == "account_session_invalid"
    finally:
        f.close()


def test_fetch_resets_error_state_on_success(monkeypatch):
    """A successful fetch must clear both attributes so a transient
    failure doesn't leave the auth-failure flag latched forever."""
    payload = {
        "five_hour": {"utilization": 12.5, "resets_at": "2026-06-03T01:00:00Z"},
        "seven_day": {"utilization": 7.0, "resets_at": "2026-06-08T00:00:00Z"},
    }

    class _R:
        status_code = 200
        text = "ok"

        def json(self_inner):
            return payload

    class _MC:
        def __init__(self, **kwargs):
            self.headers = {}
            self.cookies = {}

        def get(self, url, **kwargs):
            return _R()

        def close(self):
            pass

    monkeypatch.setattr(claude_api.httpx, "Client", _MC)
    f = claude_api.ClaudeUsageFetcher(
        {"sessionKey": "x"}, user_agent="ua",
        org_id="00000000-0000-0000-0000-000000000000",
    )
    try:
        # Pre-seed with a stale failure state to prove fetch() clears it.
        f.last_status_code = 403
        f.last_error_code = "account_session_invalid"
        result = f.fetch()
        assert result is not None
        assert f.last_status_code is None
        assert f.last_error_code is None
    finally:
        f.close()
