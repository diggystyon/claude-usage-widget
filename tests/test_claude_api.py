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
