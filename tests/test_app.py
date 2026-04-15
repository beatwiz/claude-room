"""Tests for the claude-tools-dashboard Flask app and its helpers."""

import json

import pytest


def test_import_app():
    """Smoke test: the app module imports cleanly and exposes its Flask app."""
    import app

    assert app.app is not None
    assert hasattr(app, "_collector")


# --- _flatten_snapshot contract ---

# Keys in the stable contract, in order. Any response must contain exactly these keys.
CONTRACT_KEYS = [
    "ready",
    "timestamp",
    "claude_active",
    "session_pct",
    "session_reset",
    "weekly_pct",
    "weekly_reset",
    "weekly_reset_display",
    "sonnet_pct",
    "sonnet_reset",
    "combined_saved",
    "combined_saved_usd",
    "this_week_saved",
    "burn_rate_daily",
    "week_is_fresh",
    "rtk_active",
    "rtk_health",
    "rtk_version",
    "rtk_saved",
    "rtk_delta",
    "rtk_commands",
    "rtk_avg_pct",
    "headroom_active",
    "headroom_health",
    "headroom_version",
    "headroom_saved",
    "headroom_delta",
    "headroom_sessions",
    "headroom_session_saved",
    "headroom_lifetime_saved",
    "headroom_session_saved_usd",
    "headroom_lifetime_saved_usd",
    "headroom_cache_hit_rate",
    "headroom_requests_total",
    "headroom_requests_failed",
    "headroom_avg_latency_ms",
    "extra_usage_enabled",
    "extra_usage_monthly_limit",
    "extra_usage_used",
    "extra_usage_pct",
    # Surface upstream freshness state to statusline consumers. "error" when
    # no successful Claude usage fetch has ever landed, "stale" when Headroom's
    # subscription_window poller stopped refreshing (the values are frozen from
    # an earlier successful poll), "ok" when the latest poll is fresh.
    "claude_usage_health",
    "claude_usage_polled_at",
]


def test_flatten_snapshot_none_returns_ready_false():
    """When the collector has not ticked yet, return a stable not-ready shape."""
    import app

    flat = app._flatten_snapshot(None)

    # Stable shape: every contract key is present, nothing extra.
    assert set(flat.keys()) == set(CONTRACT_KEYS)

    # Not ready
    assert flat["ready"] is False
    assert flat["timestamp"] is None
    assert flat["combined_saved_usd"] is None

    # Claude fields are null (unknown != zero)
    assert flat["claude_active"] is False
    assert flat["session_pct"] is None
    assert flat["session_reset"] is None
    assert flat["weekly_pct"] is None
    assert flat["weekly_reset"] is None
    assert flat["weekly_reset_display"] is None
    assert flat["sonnet_pct"] is None
    assert flat["sonnet_reset"] is None

    # Counter fields are zero
    assert flat["combined_saved"] == 0
    assert flat["this_week_saved"] == 0
    assert flat["burn_rate_daily"] == 0
    assert flat["week_is_fresh"] is False

    # Each tool's common fields
    for tool in ("rtk", "headroom"):
        assert flat[f"{tool}_active"] is False
        assert flat[f"{tool}_health"] == "error"
        assert flat[f"{tool}_version"] == "unknown"
        assert flat[f"{tool}_saved"] == 0
        assert flat[f"{tool}_delta"] == 0

    # Tool-specific defaults
    assert flat["rtk_commands"] == 0
    assert flat["rtk_avg_pct"] == 0
    assert flat["headroom_sessions"] == 0

    # headroom lifetime/session default to 0 when not ready
    assert flat["headroom_session_saved"] == 0
    assert flat["headroom_lifetime_saved"] == 0
    assert flat["headroom_session_saved_usd"] == 0
    assert flat["headroom_lifetime_saved_usd"] == 0
    assert flat["headroom_cache_hit_rate"] == 0
    assert flat["headroom_requests_total"] == 0
    assert flat["headroom_requests_failed"] == 0
    assert flat["headroom_avg_latency_ms"] == 0

    # extra_usage defaults when not ready
    assert flat["extra_usage_enabled"] is False
    assert flat["extra_usage_monthly_limit"] is None
    assert flat["extra_usage_used"] is None
    assert flat["extra_usage_pct"] is None


def test_flatten_snapshot_has_no_jcode_jdoc_keys():
    """_flatten_snapshot must not produce any jcodemunch or jdocmunch keys."""
    import app

    flat = app._flatten_snapshot(None)

    jcode_keys = [k for k in flat if "jcodemunch" in k or "jdocmunch" in k]
    assert jcode_keys == [], f"unexpected keys: {jcode_keys}"


# Hand-built "full" snapshot used by several tests below. Every field the
# flattener reads from is populated with a distinct, recognisable value so
# a wrong mapping produces a wrong assertion.
FULL_SNAP = {
    "timestamp": "2026-04-13T10:37:47.613296+00:00",
    "combined_saved": 123456,
    "claude_usage": {
        "active": True,
        "session_pct": 42,
        # All reset timestamps are pinned far in the future so the contract
        # tests stay green as wall-clock time marches past the original 2026
        # fixture dates. _flatten_snapshot drops pct/reset fields whose reset
        # window has already passed (see test_flatten_snapshot_drops_stale_*).
        "session_reset": "2099-04-13T15:00:00+00:00",
        "weekly_pct": 18,
        "weekly_reset": "2099-04-17T15:00:00+00:00",
        "sonnet_pct": 6,
        "sonnet_reset": "2099-04-17T15:00:00+00:00",
        "extra_usage_enabled": True,
        "extra_usage_monthly_limit": 17000,
        "extra_usage_used": 6072.0,
        "extra_usage_pct": 35.71764705882353,
    },
    "weekly": {
        "this_week": 8000,
        "burn_rate_daily": 1200,
        "reset_display": "Thu 17 Apr 15:00",
        "week_is_fresh": False,
    },
    "rtk": {
        "active": True,
        "health": "ok",
        "version": "0.3.1",
        "total_saved": 50000,
        "total_commands": 1234,
        "avg_savings_pct": 73.5,
    },
    "headroom": {
        "active": True,
        "health": "ok",
        "version": "1.0.0",
        "total_saved": 40000,
        "sessions": 3,
        "session_saved": 67861473,
        "lifetime_saved": 117309038,
        "session_saved_usd": 339.22,
        "lifetime_saved_usd": 584.41,
        "cache_hit_rate": 71.8,
        "requests_total": 1586,
        "requests_failed": 0,
        "avg_latency_ms": 7477.5,
    },
    "sparklines": {
        "rtk": {"delta": 42, "points": []},
        "headroom": {"delta": 10, "points": []},
    },
}


def test_flatten_snapshot_full_payload():
    """Happy path: full snapshot maps to every contract key with the expected values."""
    import app

    flat = app._flatten_snapshot(FULL_SNAP)

    assert set(flat.keys()) == set(CONTRACT_KEYS)

    assert flat["ready"] is True
    assert flat["timestamp"] == "2026-04-13T10:37:47.613296+00:00"

    # Claude usage
    assert flat["claude_active"] is True
    assert flat["session_pct"] == 42
    assert flat["session_reset"] == "2099-04-13T15:00:00+00:00"
    assert flat["weekly_pct"] == 18
    assert flat["weekly_reset"] == "2099-04-17T15:00:00+00:00"
    assert flat["weekly_reset_display"] == "Thu 17 Apr 15:00"
    assert flat["sonnet_pct"] == 6
    assert flat["sonnet_reset"] == "2099-04-17T15:00:00+00:00"

    # Combined/weekly savings
    assert flat["combined_saved"] == 123456
    # rate = 584.41 / 117309038; combined_usd = 584.41 + rtk_saved*rate
    assert flat["combined_saved_usd"] == pytest.approx(584.41 + 50000 * (584.41 / 117309038))
    assert flat["this_week_saved"] == 8000
    assert flat["burn_rate_daily"] == 1200
    assert flat["week_is_fresh"] is False

    # rtk
    assert flat["rtk_active"] is True
    assert flat["rtk_health"] == "ok"
    assert flat["rtk_version"] == "0.3.1"
    assert flat["rtk_saved"] == 50000
    assert flat["rtk_delta"] == 42
    assert flat["rtk_commands"] == 1234
    assert flat["rtk_avg_pct"] == 73.5

    # headroom
    assert flat["headroom_active"] is True
    assert flat["headroom_health"] == "ok"
    assert flat["headroom_version"] == "1.0.0"
    assert flat["headroom_saved"] == 40000
    assert flat["headroom_delta"] == 10
    assert flat["headroom_sessions"] == 3

    # extra_usage
    assert flat["extra_usage_enabled"] is True
    assert flat["extra_usage_monthly_limit"] == 17000
    assert flat["extra_usage_used"] == 6072.0
    assert flat["extra_usage_pct"] == 35.71764705882353

    # headroom lifetime + session richer fields
    assert flat["headroom_session_saved"] == 67861473
    assert flat["headroom_lifetime_saved"] == 117309038
    assert flat["headroom_session_saved_usd"] == 339.22
    assert flat["headroom_lifetime_saved_usd"] == 584.41
    assert flat["headroom_cache_hit_rate"] == 71.8
    assert flat["headroom_requests_total"] == 1586
    assert flat["headroom_requests_failed"] == 0
    assert flat["headroom_avg_latency_ms"] == 7477.5


def test_flatten_snapshot_inactive_claude_usage():
    """When claude_usage.active is False, all six claude fields are None (not zero)."""
    import app

    snap = dict(FULL_SNAP)
    snap["claude_usage"] = {"active": False}

    flat = app._flatten_snapshot(snap)

    assert flat["ready"] is True  # collector has ticked; claude just hasn't replied
    assert flat["session_pct"] is None
    assert flat["session_reset"] is None
    assert flat["weekly_pct"] is None
    assert flat["weekly_reset"] is None
    assert flat["sonnet_pct"] is None
    assert flat["sonnet_reset"] is None
    # weekly_reset_display is derived from snap["weekly"]["reset_display"], which is still present
    assert flat["weekly_reset_display"] == "Thu 17 Apr 15:00"

    # extra_usage fields also null when claude_usage is inactive
    assert flat["extra_usage_enabled"] is False
    assert flat["extra_usage_monthly_limit"] is None
    assert flat["extra_usage_used"] is None
    assert flat["extra_usage_pct"] is None


def test_flatten_snapshot_missing_sparklines():
    """When the sparklines key is missing, all *_delta default to 0 without raising."""
    import app

    snap = {k: v for k, v in FULL_SNAP.items() if k != "sparklines"}

    flat = app._flatten_snapshot(snap)

    assert flat["rtk_delta"] == 0
    assert flat["headroom_delta"] == 0
    # Other rtk fields still work
    assert flat["rtk_saved"] == 50000


def test_flatten_snapshot_missing_weekly():
    """When the weekly key is missing, weekly fields default cleanly."""
    import app

    snap = {k: v for k, v in FULL_SNAP.items() if k != "weekly"}

    flat = app._flatten_snapshot(snap)

    assert flat["this_week_saved"] == 0
    assert flat["burn_rate_daily"] == 0
    assert flat["week_is_fresh"] is False
    assert flat["weekly_reset_display"] is None


def test_flatten_snapshot_extra_usage_disabled():
    """When claude_usage is active but extra_usage is disabled, the four extra_usage fields are default."""
    import app

    snap = dict(FULL_SNAP)
    snap["claude_usage"] = dict(FULL_SNAP["claude_usage"])
    snap["claude_usage"]["extra_usage_enabled"] = False
    snap["claude_usage"]["extra_usage_monthly_limit"] = None
    snap["claude_usage"]["extra_usage_used"] = None
    snap["claude_usage"]["extra_usage_pct"] = None

    flat = app._flatten_snapshot(snap)

    # Other claude fields still populated
    assert flat["session_pct"] == 42
    # Extra usage is off
    assert flat["extra_usage_enabled"] is False
    assert flat["extra_usage_monthly_limit"] is None
    assert flat["extra_usage_used"] is None
    assert flat["extra_usage_pct"] is None


# --- /api/status route ---

import json as _json


def test_status_route_happy_path(monkeypatch):
    """GET /api/status returns a flat JSON projection when the collector has a snapshot."""
    import app

    monkeypatch.setattr(app._collector, "snapshot", lambda: FULL_SNAP)

    client = app.app.test_client()
    resp = client.get("/api/status")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert resp.headers.get("Cache-Control") == "no-cache"

    body = _json.loads(resp.data)
    assert set(body.keys()) == set(CONTRACT_KEYS)
    assert body["ready"] is True
    assert body["session_pct"] == 42
    assert body["combined_saved"] == 123456
    assert body["rtk_saved"] == 50000
    assert body["headroom_lifetime_saved"] == 117309038


def test_status_route_not_ready(monkeypatch):
    """GET /api/status returns a stable shape with ready=false before the first tick."""
    import app

    monkeypatch.setattr(app._collector, "snapshot", lambda: None)

    client = app.app.test_client()
    resp = client.get("/api/status")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    body = _json.loads(resp.data)
    assert set(body.keys()) == set(CONTRACT_KEYS)
    assert body["ready"] is False
    assert body["session_pct"] is None
    assert body["combined_saved"] == 0
    assert body["rtk_health"] == "error"


# --- _same_reset_window ---


def test_weekly_cache_migration_drops_pre_v2_schema(tmp_path, monkeypatch):
    """An old-schema weekly cache must be discarded so the new baseline can re-seed."""
    import app

    cache_dir = tmp_path / "dash"
    cache_dir.mkdir()
    cache_path = cache_dir / "weekly.json"
    cache_path.write_text('{"current_week_baseline": 12345, "last_week_savings": 678}')
    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(cache_dir))

    loaded = app._load_weekly_cache()

    assert loaded == {}


def test_weekly_cache_save_stamps_schema_version(tmp_path, monkeypatch):
    """Saves always include the current schema version so load() accepts them."""
    import app
    import json as _json_mod

    cache_dir = tmp_path / "dash"
    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(cache_dir))

    app._save_weekly_cache({"current_week_baseline": 100, "last_week_savings": 0})

    cache_path = cache_dir / "weekly.json"
    saved = _json_mod.loads(cache_path.read_text())
    assert saved["schema_version"] == app.WEEKLY_CACHE_SCHEMA_VERSION
    assert saved["current_week_baseline"] == 100

    # And load() accepts it and returns the full payload.
    loaded = app._load_weekly_cache()
    assert loaded["current_week_baseline"] == 100


def test_flatten_snapshot_no_usd_when_headroom_usd_missing():
    """If headroom reports lifetime_saved but no lifetime_saved_usd, rate is unknown — don't synthesize $0."""
    import app

    snap = {
        "timestamp": "2026-04-13T10:37:47+00:00",
        "combined_saved": 100000,
        "headroom": {
            "active": True,
            "health": "ok",
            "version": "0.4.0",
            "total_saved": 50000,
            "lifetime_saved": 100000,
            "lifetime_saved_usd": 0,
        },
        "rtk": {"active": True, "total_saved": 50000, "health": "ok", "version": "0.3.1"},
        "claude_usage": {"active": False},
        "weekly": {},
        "sparklines": {},
    }

    flat = app._flatten_snapshot(snap)

    assert flat["combined_saved_usd"] is None


def test_collect_headroom_uses_recent_requests_for_history(monkeypatch):
    """Headroom activity feed should reflect recent proxy requests even with zero savings."""
    import app

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

    stats_payload = {
        "tokens": {"saved": 0, "savings_percent": 0},
        "compression_cache": {},
        "display_session": {"tokens_saved": 0, "compression_savings_usd": 0},
        "persistent_savings": {"lifetime": {"tokens_saved": 0, "compression_savings_usd": 0}},
        "requests": {"total": 3, "failed": 0},
        "latency": {"average_ms": 321.4},
        "prefix_cache": {"totals": {"hit_rate": 0}},
        "recent_requests": [
            {
                "timestamp": "2026-04-14T13:20:00+00:00",
                "model": "gpt-5.4",
                "input_tokens_original": 120,
                "input_tokens_optimized": 120,
                "tokens_saved": 0,
                "savings_percent": 0,
            }
        ],
    }

    monkeypatch.setattr(app, "_headroom_version", "1.0.0")
    monkeypatch.setattr(app, "_headroom_last_total", 0)
    monkeypatch.setattr(app, "_headroom_history", [])
    monkeypatch.setattr(app, "urlopen", lambda url, timeout=2: _Resp(stats_payload))

    data = app.collect_headroom()

    assert data["active"] is True
    assert data["requests_total"] == 3
    assert data["history"] == [
        {
            "time": "2026-04-14T13:20:00+00:00",
            "tool": "headroom",
            "cmd": "gpt-5.4 120 input tokens",
            "saved_tokens": 0,
            "saved_pct": 0,
        }
    ]


def test_collect_headroom_prefers_internal_history_over_recent_requests(monkeypatch):
    """Headroom's /stats recent_requests list can be stale (kept to a tiny ring
    buffer upstream) while the dashboard has already recorded every compression
    event in _headroom_history via delta-on-total_saved. The feed must surface
    those internally-tracked events, not the sparse upstream list, otherwise
    fresh activity never shows up until recent_requests happens to rotate."""
    import app

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
        def read(self):
            return json.dumps(self._payload).encode()

    # Seed an initial total so the first delta is captured.
    monkeypatch.setattr(app, "_headroom_last_total", 1000)
    monkeypatch.setattr(app, "_headroom_history", [])
    monkeypatch.setattr(app, "_headroom_version", "1.0.0")

    # Headroom response: recent_requests has ONE stale entry, but total_saved
    # has jumped by 80000 — a new compression event we should detect.
    payload = {
        "tokens": {"saved": 81000, "savings_percent": 42.0},
        "display_session": {"tokens_saved": 81000},
        "persistent_savings": {"lifetime": {"tokens_saved": 81000}},
        "requests": {"total": 20, "failed": 0},
        "latency": {"average_ms": 100},
        "prefix_cache": {"totals": {"hit_rate": 0}},
        "recent_requests": [{
            "timestamp": "2026-04-14T19:15:40.583031",
            "model": "claude-haiku-4-5-20251001",
            "input_tokens_original": 9,
            "input_tokens_optimized": 8,
            "tokens_saved": 1,
            "savings_percent": 11.1,
        }],
    }
    monkeypatch.setattr(app, "urlopen", lambda url, timeout=2: _Resp(payload))

    result = app.collect_headroom()
    assert result is not None
    feed = result["history"]

    # The feed must contain the internally-detected 80k compression event,
    # not just the stale 1-token recent_requests entry.
    big_events = [e for e in feed if e.get("saved_tokens", 0) >= 80000]
    assert len(big_events) >= 1, f"feed should contain the 80k delta event, got {feed}"


def test_normalize_iso_ts_passes_through_explicit_utc():
    """Timestamps that already carry a UTC offset must round-trip unchanged."""
    import app
    from datetime import timezone, timedelta

    lisbon = timezone(timedelta(hours=1))
    result = app._normalize_iso_ts("2026-04-14T18:45:09.546274+00:00", lisbon)
    assert result == "2026-04-14T18:45:09.546274+00:00"


def test_normalize_iso_ts_applies_assumed_tz_to_naive():
    """A naive ISO string must be interpreted in the assumed tz and converted to UTC.
    This is the fix for headroom's upstream bug of emitting naive local timestamps
    in recent_requests/savings_history while other fields use explicit UTC."""
    import app
    from datetime import timezone, timedelta

    lisbon = timezone(timedelta(hours=1))  # WEST = UTC+1
    # Headroom's naive "19:15:40" in Lisbon == "18:15:40" in UTC
    result = app._normalize_iso_ts("2026-04-14T19:15:40.583031", lisbon)
    assert result == "2026-04-14T18:15:40.583031+00:00"


def test_normalize_iso_ts_handles_unparseable_input():
    """Garbage strings should pass through unchanged instead of raising."""
    import app
    from datetime import timezone, timedelta

    assert app._normalize_iso_ts("not-a-date", timezone(timedelta(hours=1))) == "not-a-date"
    assert app._normalize_iso_ts(None, timezone.utc) is None
    assert app._normalize_iso_ts("", timezone.utc) == ""


def test_format_headroom_recent_requests_normalizes_naive_timestamp(monkeypatch):
    """When headroom emits a naive timestamp, _format_headroom_recent_requests must
    normalize it so the server-side sort by string equals a chronological sort."""
    import app
    from datetime import timezone, timedelta

    lisbon = timezone(timedelta(hours=1))
    monkeypatch.setattr(app, "_HEADROOM_ASSUMED_TZ", lisbon)

    rows = app._format_headroom_recent_requests([
        {
            "timestamp": "2026-04-14T19:15:40.583031",
            "model": "claude-haiku-4-5",
            "input_tokens_original": 120,
            "input_tokens_optimized": 110,
            "tokens_saved": 10,
            "savings_percent": 8.3,
        }
    ])

    assert len(rows) == 1
    assert rows[0]["time"] == "2026-04-14T18:15:40.583031+00:00"


# --- collect_claude_usage reads from Headroom's /stats subscription_window ---
#
# History: this used to hit https://api.anthropic.com/api/oauth/usage directly
# with an OAuth token read from ~/.claude/.credentials.json, but that endpoint
# is per-token rate limited tighter than our collector's cadence and we were
# permanently 429'd. Headroom already polls the same data on a sane schedule
# and exposes it under subscription_window.latest on its /stats endpoint, so
# we just piggyback on its cache instead. No credentials needed.


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()


_HEADROOM_STATS_WITH_SUBSCRIPTION = {
    "tokens": {"saved": 0, "savings_percent": 0},
    "display_session": {"tokens_saved": 0, "compression_savings_usd": 0},
    "persistent_savings": {"lifetime": {"tokens_saved": 0, "compression_savings_usd": 0}},
    "requests": {"total": 0, "failed": 0},
    "latency": {"average_ms": 0},
    "prefix_cache": {"totals": {"hit_rate": 0}},
    "recent_requests": [],
    "subscription_window": {
        "latest": {
            "five_hour": {
                "utilization_pct": 14.0,
                "resets_at": "2026-04-15T04:00:00+00:00",
                "seconds_to_reset": 11800.0,
            },
            "seven_day": {
                "utilization_pct": 5.0,
                "resets_at": "2026-04-21T18:00:00+00:00",
                "seconds_to_reset": 580000.0,
            },
            "seven_day_sonnet": {
                "utilization_pct": 8.0,
                "resets_at": "2026-04-15T14:00:00+00:00",
                "seconds_to_reset": 47800.0,
            },
            "extra_usage": {
                "is_enabled": True,
                "monthly_limit_usd": 170.0,
                "used_credits_usd": 173.66,
                "utilization_pct": 100.0,
            },
            "polled_at": "2026-04-15T00:42:09+00:00",
        },
    },
}


def test_collect_claude_usage_reads_from_headroom_subscription_window(monkeypatch):
    """Happy path: /stats returns subscription_window.latest → map every field
    into the claude_usage contract shape that _flatten_snapshot expects."""
    import app

    monkeypatch.setattr(app, "_claude_usage_last_good", None)
    monkeypatch.setattr(
        app,
        "urlopen",
        lambda url, timeout=2: _FakeResp(_HEADROOM_STATS_WITH_SUBSCRIPTION),
    )

    result = app.collect_claude_usage()

    assert result["active"] is True
    assert result["session_pct"] == 14.0
    assert result["session_reset"] == "2026-04-15T04:00:00+00:00"
    assert result["weekly_pct"] == 5.0
    assert result["weekly_reset"] == "2026-04-21T18:00:00+00:00"
    assert result["sonnet_pct"] == 8.0
    assert result["sonnet_reset"] == "2026-04-15T14:00:00+00:00"
    # Extra usage units: USD now (was cents when we hit Anthropic direct).
    assert result["extra_usage_enabled"] is True
    assert result["extra_usage_monthly_limit"] == 170.0
    assert result["extra_usage_used"] == 173.66
    assert result["extra_usage_pct"] == 100.0


def test_collect_claude_usage_inactive_when_headroom_unreachable_on_cold_start(monkeypatch):
    """Cold start (no prior successful fetch) with Headroom unreachable →
    {'active': False}. After we've seen a successful response, the last-good
    fallback kicks in instead; that's covered by
    test_collect_claude_usage_returns_last_good_on_transient_failure."""
    import app
    from urllib.error import URLError

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    def _raise(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(app, "urlopen", _raise)

    result = app.collect_claude_usage()

    assert result == {"active": False}


def test_collect_claude_usage_inactive_when_subscription_window_missing_on_cold_start(monkeypatch):
    """Older Headroom builds may not emit subscription_window. On cold start
    that's {'active': False}. Once we've ever seen a valid payload, a later
    missing subscription_window hands back the last-good instead — same
    fallback path as a transient URL error."""
    import app

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    # /stats payload without a subscription_window key at all
    bare_stats = {
        "tokens": {"saved": 0, "savings_percent": 0},
        "requests": {"total": 0, "failed": 0},
        "latency": {"average_ms": 0},
        "prefix_cache": {"totals": {"hit_rate": 0}},
        "recent_requests": [],
    }

    monkeypatch.setattr(
        app, "urlopen", lambda url, timeout=2: _FakeResp(bare_stats)
    )

    result = app.collect_claude_usage()

    assert result == {"active": False}


def test_collect_claude_usage_returns_last_good_on_transient_failure(monkeypatch):
    """Once we've seen a successful subscription_window response, a single
    transient /stats failure (URLError / socket timeout from Headroom chewing
    on a big proxy request) must not blank the dashboard — return the
    previously cached payload instead.

    This matches the last-good pattern collect_headroom/rtk use
    via _last_good in collect_all, so the Claude Usage card stops flickering
    to '--' every time Headroom hiccups.
    """
    import app
    from urllib.error import URLError

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    # First call succeeds → should populate the last-good cache
    monkeypatch.setattr(
        app,
        "urlopen",
        lambda url, timeout=2: _FakeResp(_HEADROOM_STATS_WITH_SUBSCRIPTION),
    )
    first = app.collect_claude_usage()
    assert first["active"] is True
    assert first["session_pct"] == 14.0

    # Second call — Headroom unreachable — must return the cached result,
    # NOT {"active": False}.
    def _raise(*args, **kwargs):
        raise URLError("timed out")

    monkeypatch.setattr(app, "urlopen", _raise)
    second = app.collect_claude_usage()
    assert second["active"] is True
    assert second["session_pct"] == 14.0
    assert second == first


def test_collect_claude_usage_extra_usage_disabled(monkeypatch):
    """When extra_usage.is_enabled is false, the three extra_usage detail
    fields must be None (not zero), matching the claude_active=True but
    extra_usage=off contract that _flatten_snapshot tests already pin."""
    import app
    import copy

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    payload = copy.deepcopy(_HEADROOM_STATS_WITH_SUBSCRIPTION)
    payload["subscription_window"]["latest"]["extra_usage"] = {
        "is_enabled": False,
        "monthly_limit_usd": 0,
        "used_credits_usd": 0,
        "utilization_pct": 0,
    }

    monkeypatch.setattr(app, "urlopen", lambda url, timeout=2: _FakeResp(payload))

    result = app.collect_claude_usage()

    assert result["active"] is True
    assert result["extra_usage_enabled"] is False
    assert result["extra_usage_monthly_limit"] is None
    assert result["extra_usage_used"] is None
    assert result["extra_usage_pct"] is None
    # Other metrics still populated
    assert result["session_pct"] == 14.0


def test_collect_headroom_returns_none_on_urlerror(monkeypatch):
    """When headroom /stats is unreachable, return None so collect_all falls back
    to _last_good instead of a stub that would zero out combined_saved and
    pollute the last-good cache."""
    import app
    from urllib.error import URLError

    def _raise(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(app, "_headroom_version", "1.0.0")
    monkeypatch.setattr(app, "urlopen", _raise)

    assert app.collect_headroom() is None


def test_collect_headroom_returns_none_on_json_error(monkeypatch):
    """Malformed JSON from headroom should also return None, not a lossy stub."""
    import app

    class _Resp:
        def read(self):
            return b"not-json{"

    monkeypatch.setattr(app, "_headroom_version", "1.0.0")
    monkeypatch.setattr(app, "urlopen", lambda url, timeout=2: _Resp())

    assert app.collect_headroom() is None


def test_same_reset_window_identical_timestamps():
    """Two identical timestamps refer to the same window."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:01.339260+00:00",
        "2026-04-14T18:00:01.339260+00:00",
    ) is True


def test_same_reset_window_microsecond_drift():
    """Same minute, different microseconds — should be the same window."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:01.339260+00:00",
        "2026-04-14T18:00:01.150071+00:00",
    ) is True


def test_same_reset_window_sub_second_drift():
    """Same minute, different seconds within the minute — should still be the same window."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:01.339260+00:00",
        "2026-04-14T18:00:00.513979+00:00",
    ) is True


def test_same_reset_window_different_minutes():
    """Timestamps in different minutes are different windows — rotation should fire."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:01.339260+00:00",
        "2026-04-14T18:02:00.000000+00:00",
    ) is False


def test_same_reset_window_minute_boundary_drift():
    """Sub-second jitter straddling a minute boundary is still the same window."""
    import app

    assert app._same_reset_window(
        "2026-04-14T17:59:59.800000+00:00",
        "2026-04-14T18:00:00.200000+00:00",
    ) is True


def test_same_reset_window_outside_tolerance():
    """Drift larger than the tolerance is a real rotation."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:00+00:00",
        "2026-04-14T18:00:10+00:00",
    ) is False


def test_same_reset_window_different_weeks():
    """Timestamps a week apart are different windows."""
    import app

    assert app._same_reset_window(
        "2026-04-14T18:00:01+00:00",
        "2026-04-21T18:00:01+00:00",
    ) is False


def test_same_reset_window_none_or_empty():
    """Missing timestamps are not a match."""
    import app

    assert app._same_reset_window(None, "2026-04-14T18:00:01+00:00") is False
    assert app._same_reset_window("2026-04-14T18:00:01+00:00", None) is False
    assert app._same_reset_window("", "2026-04-14T18:00:01+00:00") is False
    assert app._same_reset_window(None, None) is False


def test_same_reset_window_invalid_format_falls_back_to_string_equality():
    """An unparseable string still works via string-equality fallback."""
    import app

    # Both identical garbage strings → True via fallback
    assert app._same_reset_window("not-a-date", "not-a-date") is True
    # Different garbage → False
    assert app._same_reset_window("not-a-date", "other-garbage") is False


# --- stale reset-window scrubbing in _flatten_snapshot ---
#
# When the Anthropic usage API rate-limits us, collect_claude_usage() keeps
# serving its last successful response. If the 5-hour session window (or the
# weekly / sonnet window) has rolled over in the meantime, the cached
# utilization refers to a dead window and must not be shown to the user.
# _flatten_snapshot is responsible for scrubbing stale pct/reset pairs so the
# frontend renders "--" instead of lying with ghost data.


def test_flatten_snapshot_drops_stale_session_window():
    """session_reset in the past → session_pct / session_reset are None,
    weekly and sonnet untouched."""
    import app
    import copy

    snap = copy.deepcopy(FULL_SNAP)
    snap["claude_usage"]["session_reset"] = "2020-01-01T00:00:00+00:00"

    flat = app._flatten_snapshot(snap)

    assert flat["session_pct"] is None
    assert flat["session_reset"] is None
    # Other Claude metrics still pass through
    assert flat["claude_active"] is True
    assert flat["weekly_pct"] == 18
    assert flat["weekly_reset"] == "2099-04-17T15:00:00+00:00"
    assert flat["sonnet_pct"] == 6
    assert flat["sonnet_reset"] == "2099-04-17T15:00:00+00:00"


def test_flatten_snapshot_drops_stale_sonnet_window():
    """sonnet_reset in the past → sonnet_pct / sonnet_reset are None,
    session and weekly untouched."""
    import app
    import copy

    snap = copy.deepcopy(FULL_SNAP)
    snap["claude_usage"]["sonnet_reset"] = "2020-01-01T00:00:00+00:00"

    flat = app._flatten_snapshot(snap)

    assert flat["sonnet_pct"] is None
    assert flat["sonnet_reset"] is None
    assert flat["session_pct"] == 42
    assert flat["weekly_pct"] == 18


def test_flatten_snapshot_drops_stale_weekly_window():
    """weekly_reset in the past → weekly_pct / weekly_reset /
    weekly_reset_display are all None. The display string is also derived
    from the same stale cache, so it can't be trusted either."""
    import app
    import copy

    snap = copy.deepcopy(FULL_SNAP)
    snap["claude_usage"]["weekly_reset"] = "2020-01-01T00:00:00+00:00"

    flat = app._flatten_snapshot(snap)

    assert flat["weekly_pct"] is None
    assert flat["weekly_reset"] is None
    assert flat["weekly_reset_display"] is None
    # Other metrics still populated
    assert flat["session_pct"] == 42
    assert flat["sonnet_pct"] == 6


def test_flatten_snapshot_inactive_claude_passes_through_weekly_reset_display():
    """When claude_active=False, weekly_reset_display still passes through
    from snap['weekly']['reset_display'] because it reflects an older
    successful fetch, not a stale current window. Regression guard against
    the stale-window scrubbing also killing the inactive-claude path."""
    import app

    snap = dict(FULL_SNAP)
    snap["claude_usage"] = {"active": False}
    flat = app._flatten_snapshot(snap)

    assert flat["claude_active"] is False
    assert flat["weekly_reset_display"] == "Thu 17 Apr 15:00"


# --- reset display formatting (local time) ---


def test_format_claude_reset_converts_utc_to_explicit_local_tz():
    """_format_claude_reset must convert a UTC-aware ISO string to the
    supplied local timezone before formatting. Prevents the dashboard from
    showing UTC wall-clock times to the user."""
    import app
    from zoneinfo import ZoneInfo

    lisbon = ZoneInfo("Europe/Lisbon")
    # 2026-04-21T18:00:00 UTC → WEST (UTC+1 in April) → 19:00 local
    assert (
        app._format_claude_reset("2026-04-21T18:00:00+00:00", local_tz=lisbon)
        == "Tue 21 Apr 19:00"
    )

    nyc = ZoneInfo("America/New_York")
    # Same UTC instant → EDT (UTC-4 in April) → 14:00 local
    assert (
        app._format_claude_reset("2026-04-21T18:00:00+00:00", local_tz=nyc)
        == "Tue 21 Apr 14:00"
    )


def test_format_claude_reset_handles_empty_and_invalid():
    """Empty / None / garbage inputs return an empty string instead of
    raising."""
    import app

    assert app._format_claude_reset(None) == ""
    assert app._format_claude_reset("") == ""
    assert app._format_claude_reset("not-a-timestamp") == ""


# ---------------------------------------------------------------------------
# Codex P1: share Headroom /stats payload between collect_headroom and
# collect_claude_usage so collect_all only fetches /stats once per tick.
# ---------------------------------------------------------------------------


def test_collect_claude_usage_reuses_shared_stats_payload(monkeypatch):
    """When a shared /stats payload is passed in via stats_raw, the collector
    must parse it directly instead of making its own urlopen call. This
    eliminates the duplicate /stats request collect_all was making every tick
    (240/min at COLLECTOR_INTERVAL=0.25 with a 3s timeout path)."""
    import app

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            "urlopen must not be called when stats_raw is provided"
        )
    monkeypatch.setattr(app, "urlopen", _should_not_be_called)

    result = app.collect_claude_usage(
        stats_raw=_HEADROOM_STATS_WITH_SUBSCRIPTION
    )

    assert result["active"] is True
    assert result["session_pct"] == 14.0
    assert result["weekly_pct"] == 5.0
    assert result["sonnet_pct"] == 8.0


def test_collect_headroom_reuses_shared_stats_payload(monkeypatch):
    """collect_headroom must accept a pre-fetched /stats payload so collect_all
    can share one fetch between collect_headroom and collect_claude_usage."""
    import app

    monkeypatch.setattr(app, "_headroom_version", "1.2.3")
    monkeypatch.setattr(app, "_headroom_last_total", 0)
    monkeypatch.setattr(app, "_headroom_history", [])

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            "urlopen must not be called when stats_raw is provided"
        )
    monkeypatch.setattr(app, "urlopen", _should_not_be_called)

    data = app.collect_headroom(stats_raw=_HEADROOM_STATS_WITH_SUBSCRIPTION)

    assert data is not None
    assert data["active"] is True
    assert data["version"] == "1.2.3"


def test_collect_all_fetches_headroom_stats_once_per_cycle(monkeypatch, tmp_path):
    """collect_all must perform at most one /stats fetch per tick. Previously
    collect_headroom and collect_claude_usage each issued their own, doubling
    the request rate and compounding latency when Headroom was slow."""
    import app
    import json as _json

    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(app, "_headroom_version", "1.2.3")
    monkeypatch.setattr(app, "_headroom_last_total", 0)
    monkeypatch.setattr(app, "_headroom_history", [])
    monkeypatch.setattr(app, "_claude_usage_last_good", None)
    for name in app._sparkline_buffers:
        app._sparkline_buffers[name].clear()
    for name in app._last_collect_success:
        app._last_collect_success[name] = 0.0
    app._last_good.clear()

    stats_calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return _json.dumps(self._payload).encode()

    def _fake_urlopen(url, timeout=2):
        if url.endswith("/stats"):
            stats_calls["n"] += 1
        return _Resp(_HEADROOM_STATS_WITH_SUBSCRIPTION)

    monkeypatch.setattr(app, "urlopen", _fake_urlopen)
    monkeypatch.setattr(app, "collect_rtk", lambda: {"active": False})

    app.collect_all()

    assert stats_calls["n"] == 1, (
        f"collect_all fired {stats_calls['n']} /stats fetches per tick; "
        "expected exactly 1 (shared between collect_headroom and collect_claude_usage)"
    )


# ---------------------------------------------------------------------------
# Codex P2: surface stale Claude usage when Headroom's subscription_window
# poller hasn't refreshed recently (credentials expired, upstream errors).
# The card must not stay forever-green once fresh data stops arriving.
# ---------------------------------------------------------------------------


def _with_fresh_polled_at(payload, polled_at):
    """Deep-copy _HEADROOM_STATS_WITH_SUBSCRIPTION with a custom polled_at."""
    import copy as _copy

    clone = _copy.deepcopy(payload)
    clone["subscription_window"]["latest"]["polled_at"] = polled_at
    return clone


def test_collect_claude_usage_marks_health_ok_when_polled_at_is_recent(monkeypatch):
    """When Headroom's subscription_window was polled within the freshness
    window, the returned payload is healthy."""
    import app
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    fake_now = datetime(2026, 4, 15, 1, 50, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(app, "_utc_now", lambda: fake_now)

    fresh_polled_at = (fake_now - timedelta(seconds=30)).isoformat()
    payload = _with_fresh_polled_at(
        _HEADROOM_STATS_WITH_SUBSCRIPTION, fresh_polled_at
    )

    result = app.collect_claude_usage(stats_raw=payload)

    assert result["active"] is True
    assert result["health"] == "ok"
    assert result["polled_at"] == fresh_polled_at


def test_collect_claude_usage_marks_health_stale_when_polled_at_is_old(monkeypatch):
    """When Headroom's subscription_window poller has stopped refreshing
    (poll_errors climbing, credentials expired, upstream 4xx), the values we
    show are frozen. The result must carry health='stale' so the dashboard
    can stop presenting the Claude Usage card as forever-green."""
    import app
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    fake_now = datetime(2026, 4, 15, 1, 50, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(app, "_utc_now", lambda: fake_now)

    # 15 minutes past — way beyond a reasonable poll gap.
    old_polled_at = (fake_now - timedelta(minutes=15)).isoformat()
    payload = _with_fresh_polled_at(
        _HEADROOM_STATS_WITH_SUBSCRIPTION, old_polled_at
    )

    result = app.collect_claude_usage(stats_raw=payload)

    assert result["active"] is True  # cached values are still usable
    assert result["health"] == "stale"
    assert result["polled_at"] == old_polled_at


def test_collect_claude_usage_stale_when_polled_at_is_missing(monkeypatch):
    """Legacy Headroom responses without polled_at are treated as stale —
    we can't prove freshness, so err on the side of signalling it."""
    import app
    import copy as _copy

    monkeypatch.setattr(app, "_claude_usage_last_good", None)
    payload = _copy.deepcopy(_HEADROOM_STATS_WITH_SUBSCRIPTION)
    payload["subscription_window"]["latest"].pop("polled_at", None)

    result = app.collect_claude_usage(stats_raw=payload)

    assert result["active"] is True
    assert result["health"] == "stale"
    assert result["polled_at"] is None


def test_flatten_snapshot_propagates_claude_usage_health_and_polled_at():
    """_flatten_snapshot must forward claude_usage.health and claude_usage.polled_at
    as flat contract keys so statusline consumers can detect upstream freshness."""
    import app
    import copy as _copy

    snap = _copy.deepcopy(FULL_SNAP)
    snap["claude_usage"]["health"] = "stale"
    snap["claude_usage"]["polled_at"] = "2026-04-15T00:42:09+00:00"

    flat = app._flatten_snapshot(snap)

    assert flat["claude_usage_health"] == "stale"
    assert flat["claude_usage_polled_at"] == "2026-04-15T00:42:09+00:00"


def test_flatten_snapshot_claude_usage_health_defaults_error_when_inactive():
    """A missing / inactive claude_usage dict maps to health='error', not 'ok'."""
    import app
    import copy as _copy

    snap = _copy.deepcopy(FULL_SNAP)
    snap["claude_usage"] = {"active": False}

    flat = app._flatten_snapshot(snap)

    assert flat["claude_usage_health"] == "error"
    assert flat["claude_usage_polled_at"] is None


def test_flatten_snapshot_claude_usage_health_ok_when_active_and_unspecified():
    """A claude_usage dict that is active but does not carry an explicit health
    field (legacy shape) maps to 'ok' for back-compat."""
    import app

    flat = app._flatten_snapshot(FULL_SNAP)
    assert flat["claude_usage_health"] == "ok"
    assert flat["claude_usage_polled_at"] is None


# ---------------------------------------------------------------------------
# Codex P2: sparkline buffer must use lifetime_saved for headroom so a
# Headroom restart (total_saved resets, lifetime_saved persists) does not
# produce a massive negative delta/dip in the spark line.
# ---------------------------------------------------------------------------


def test_sparkline_buffer_uses_lifetime_saved_for_headroom(monkeypatch, tmp_path):
    """After a Headroom process restart, tokens.saved (total_saved) rewinds to
    near-zero while persistent_savings.lifetime.tokens_saved keeps climbing.
    The headline in collect_all and the headroom card both show
    lifetime_saved, but the sparkline buffer used total_saved — so a restart
    created a huge negative delta that made the spark line look dead.
    The buffer must feed on the same counter the headline uses."""
    import app

    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(tmp_path))
    for buf in app._sparkline_buffers.values():
        buf.clear()
    for name in app._last_collect_success:
        app._last_collect_success[name] = 0.0
    app._last_good.clear()
    monkeypatch.setattr(app, "_claude_usage_last_good", None)

    # Silence everything except headroom.
    monkeypatch.setattr(app, "collect_rtk", lambda: {"active": False})
    monkeypatch.setattr(app, "collect_claude_usage",
                        lambda stats_raw=None: {"active": False})
    monkeypatch.setattr(app, "_fetch_headroom_stats_raw", lambda: None)

    ticks = [
        {
            "active": True,
            "version": "1.0",
            "total_saved": 100_000_000,
            "lifetime_saved": 100_000_000,
            "history": [],
        },
        # Restart event: total_saved rewinds to 50, lifetime_saved keeps going.
        {
            "active": True,
            "version": "1.0",
            "total_saved": 50,
            "lifetime_saved": 100_000_050,
            "history": [],
        },
    ]
    idx = {"n": 0}

    def _fake_headroom(stats_raw=None):
        i = idx["n"]
        idx["n"] += 1
        return ticks[i]

    monkeypatch.setattr(app, "collect_headroom", _fake_headroom)

    app.collect_all()  # seeds tick 1
    snap = app.collect_all()  # tick 2, across restart

    hr_delta = snap["sparklines"]["headroom"]["delta"]
    assert hr_delta == 50, (
        f"Expected +50 lifetime_saved delta across a Headroom restart, "
        f"got {hr_delta} (sparkline buffer still reading total_saved)."
    )


# ---------------------------------------------------------------------------
# Codex P1: weekly cache must drop baselines written under a stale
# combined_saved formula. v2 and v3 both included jcodemunch/jdocmunch
# totals; this branch removed them, so pre-v4 baselines overstate the
# starting point and must be invalidated to force a fresh re-seed.
# ---------------------------------------------------------------------------


def test_weekly_cache_pre_v4_baselines_are_dropped(tmp_path, monkeypatch):
    """v2 and v3 baselines were written when combined_saved still included
    jcodemunch + jdocmunch, so their baselines are incomparable to the
    current lifetime_saved + rtk-only formula. Load must return {} so
    collect_all re-seeds from the current combined_saved value."""
    import app
    import json as _json

    cache_dir = tmp_path / "dash"
    cache_dir.mkdir()
    cache_path = cache_dir / "weekly.json"
    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(cache_dir))

    for stale_version in (2, 3):
        cache_path.write_text(_json.dumps({
            "current_week_baseline": 14847609,
            "current_week_start": "2026-04-14T18:30:05+00:00",
            "weekly_reset_at": "2026-04-21T18:00:00+00:00",
            "schema_version": stale_version,
        }))
        assert app._load_weekly_cache() == {}


def test_weekly_cache_drops_mismatched_definition_fingerprint(tmp_path, monkeypatch):
    """A v3+ cache whose combined_saved_definition no longer matches the
    current formula must be dropped — the baseline is not comparable and
    would produce wrong this-week numbers."""
    import app
    import json as _json

    cache_dir = tmp_path / "dash"
    cache_dir.mkdir()
    cache_path = cache_dir / "weekly.json"
    cache_path.write_text(_json.dumps({
        "current_week_baseline": 42,
        "schema_version": app.WEEKLY_CACHE_SCHEMA_VERSION,
        "combined_saved_definition": "old-formula-that-no-longer-exists",
    }))
    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(cache_dir))

    assert app._load_weekly_cache() == {}


def test_weekly_cache_save_stamps_combined_saved_definition(tmp_path, monkeypatch):
    """_save_weekly_cache must stamp both schema_version and the combined
    _saved formula fingerprint so subsequent loads can verify compatibility."""
    import app
    import json as _json

    cache_dir = tmp_path / "dash"
    monkeypatch.setattr(app, "WEEKLY_CACHE_DIR", str(cache_dir))

    app._save_weekly_cache({"current_week_baseline": 200, "last_week_savings": 0})

    saved = _json.loads((cache_dir / "weekly.json").read_text())
    assert saved["schema_version"] == app.WEEKLY_CACHE_SCHEMA_VERSION
    assert saved["combined_saved_definition"] == app.COMBINED_SAVED_DEFINITION
    assert saved["current_week_baseline"] == 200


# ---------------------------------------------------------------------------
# Codex P2: build.sh must pass $PORT into the container env, otherwise a
# PORT=NNNN override publishes NNNN but the app still listens on 8095
# inside the container.
# ---------------------------------------------------------------------------


def test_buildsh_propagates_port_override_to_container_env():
    """build.sh advertises PORT as overridable (line 24). The docker run
    invocation must set -e PORT="$PORT" so the Flask app inside the container
    actually binds to the overridden port, not the default 8095."""
    import os

    buildsh = os.path.join(os.path.dirname(__file__), "..", "build.sh")
    with open(buildsh) as f:
        content = f.read()

    assert '-p "$PORT:$PORT"' in content, "docker -p port publish must be present"
    assert '-e PORT="$PORT"' in content, (
        "docker -e PORT must be present so the container listens on the overridden port"
    )

