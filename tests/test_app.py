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
