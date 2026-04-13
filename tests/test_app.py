"""Tests for the claude-tools-dashboard Flask app and its helpers."""


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
    "session_pct",
    "session_reset",
    "weekly_pct",
    "weekly_reset",
    "weekly_reset_display",
    "sonnet_pct",
    "sonnet_reset",
    "combined_saved",
    "this_week_saved",
    "last_week_saved",
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
    "jcodemunch_active",
    "jcodemunch_health",
    "jcodemunch_version",
    "jcodemunch_saved",
    "jcodemunch_delta",
    "jcodemunch_repos_indexed",
    "jcodemunch_index_size_mb",
    "jcodemunch_freshness",
    "jcodemunch_freshness_label",
    "jdocmunch_active",
    "jdocmunch_health",
    "jdocmunch_version",
    "jdocmunch_saved",
    "jdocmunch_delta",
    "jdocmunch_docs_indexed",
    "jdocmunch_index_size_mb",
    "jdocmunch_freshness",
    "jdocmunch_freshness_label",
    "extra_usage_enabled",
    "extra_usage_monthly_limit",
    "extra_usage_used",
    "extra_usage_pct",
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

    # Claude fields are null (unknown != zero)
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
    assert flat["last_week_saved"] == 0
    assert flat["burn_rate_daily"] == 0
    assert flat["week_is_fresh"] is False

    # Each tool's common fields
    for tool in ("rtk", "headroom", "jcodemunch", "jdocmunch"):
        assert flat[f"{tool}_active"] is False
        assert flat[f"{tool}_health"] == "error"
        assert flat[f"{tool}_version"] == "unknown"
        assert flat[f"{tool}_saved"] == 0
        assert flat[f"{tool}_delta"] == 0

    # Tool-specific defaults
    assert flat["rtk_commands"] == 0
    assert flat["rtk_avg_pct"] == 0
    assert flat["headroom_sessions"] == 0
    assert flat["jcodemunch_repos_indexed"] == 0
    assert flat["jcodemunch_index_size_mb"] == 0
    assert flat["jcodemunch_freshness"] == 0
    assert flat["jcodemunch_freshness_label"] == "idle"
    assert flat["jdocmunch_docs_indexed"] == 0
    assert flat["jdocmunch_index_size_mb"] == 0
    assert flat["jdocmunch_freshness"] == 0
    assert flat["jdocmunch_freshness_label"] == "idle"

    # extra_usage defaults when not ready
    assert flat["extra_usage_enabled"] is False
    assert flat["extra_usage_monthly_limit"] is None
    assert flat["extra_usage_used"] is None
    assert flat["extra_usage_pct"] is None


# Hand-built "full" snapshot used by several tests below. Every field the
# flattener reads from is populated with a distinct, recognisable value so
# a wrong mapping produces a wrong assertion.
FULL_SNAP = {
    "timestamp": "2026-04-13T10:37:47.613296+00:00",
    "combined_saved": 123456,
    "claude_usage": {
        "active": True,
        "session_pct": 42,
        "session_reset": "2026-04-13T15:00:00+00:00",
        "weekly_pct": 18,
        "weekly_reset": "2026-04-17T15:00:00+00:00",
        "sonnet_pct": 6,
        "sonnet_reset": "2026-04-17T15:00:00+00:00",
        "extra_usage_enabled": True,
        "extra_usage_monthly_limit": 17000,
        "extra_usage_used": 6072.0,
        "extra_usage_pct": 35.71764705882353,
    },
    "weekly": {
        "this_week": 8000,
        "last_week": 12000,
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
    },
    "jcodemunch": {
        "active": True,
        "health": "ok",
        "version": "2.1.0",
        "total_saved": 20000,
        "repos_indexed": 12,
        "index_size_mb": 48.3,
        "freshness": 87,
        "freshness_label": "3m ago",
    },
    "jdocmunch": {
        "active": True,
        "health": "ok",
        "version": "1.0.0",
        "total_saved": 13456,
        "docs_indexed": 5,
        "index_size_mb": 4.1,
        "freshness": 40,
        "freshness_label": "24m ago",
    },
    "sparklines": {
        "rtk": {"delta": 42, "points": []},
        "headroom": {"delta": 10, "points": []},
        "jcodemunch": {"delta": 0, "points": []},
        "jdocmunch": {"delta": 0, "points": []},
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
    assert flat["session_pct"] == 42
    assert flat["session_reset"] == "2026-04-13T15:00:00+00:00"
    assert flat["weekly_pct"] == 18
    assert flat["weekly_reset"] == "2026-04-17T15:00:00+00:00"
    assert flat["weekly_reset_display"] == "Thu 17 Apr 15:00"
    assert flat["sonnet_pct"] == 6
    assert flat["sonnet_reset"] == "2026-04-17T15:00:00+00:00"

    # Combined/weekly savings
    assert flat["combined_saved"] == 123456
    assert flat["this_week_saved"] == 8000
    assert flat["last_week_saved"] == 12000
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

    # jcodemunch
    assert flat["jcodemunch_active"] is True
    assert flat["jcodemunch_health"] == "ok"
    assert flat["jcodemunch_version"] == "2.1.0"
    assert flat["jcodemunch_saved"] == 20000
    assert flat["jcodemunch_delta"] == 0
    assert flat["jcodemunch_repos_indexed"] == 12
    assert flat["jcodemunch_index_size_mb"] == 48.3
    assert flat["jcodemunch_freshness"] == 87
    assert flat["jcodemunch_freshness_label"] == "3m ago"

    # jdocmunch
    assert flat["jdocmunch_active"] is True
    assert flat["jdocmunch_health"] == "ok"
    assert flat["jdocmunch_version"] == "1.0.0"
    assert flat["jdocmunch_saved"] == 13456
    assert flat["jdocmunch_delta"] == 0
    assert flat["jdocmunch_docs_indexed"] == 5
    assert flat["jdocmunch_index_size_mb"] == 4.1
    assert flat["jdocmunch_freshness"] == 40
    assert flat["jdocmunch_freshness_label"] == "24m ago"

    # extra_usage
    assert flat["extra_usage_enabled"] is True
    assert flat["extra_usage_monthly_limit"] == 17000
    assert flat["extra_usage_used"] == 6072.0
    assert flat["extra_usage_pct"] == 35.71764705882353


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
    assert flat["jcodemunch_delta"] == 0
    assert flat["jdocmunch_delta"] == 0
    # Other rtk fields still work
    assert flat["rtk_saved"] == 50000


def test_flatten_snapshot_missing_weekly():
    """When the weekly key is missing, weekly fields default cleanly."""
    import app

    snap = {k: v for k, v in FULL_SNAP.items() if k != "weekly"}

    flat = app._flatten_snapshot(snap)

    assert flat["this_week_saved"] == 0
    assert flat["last_week_saved"] == 0
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
    assert body["jcodemunch_freshness_label"] == "3m ago"


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
