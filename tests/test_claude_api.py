"""Unit tests for claude_api.py: the shared usage-API + status + version
helpers used by both entry scripts.

These tests exercise pure-Python parsing / extraction logic. They do NOT
hit the network -- httpx clients are mocked or simply not constructed.
"""
from __future__ import annotations

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
