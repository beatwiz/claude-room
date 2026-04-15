"""Claude Tools Dashboard -- Flask backend with SSE streaming."""

import json
import os
import glob
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from dotenv import load_dotenv
from flask import Flask, Response, jsonify

load_dotenv()

app = Flask(__name__)

HOME = os.path.expanduser("~")

# Configuration via environment variables
HEADROOM_URL = os.environ.get("HEADROOM_URL", "http://127.0.0.1:8787")
RTK_DB_PATH = os.environ.get("RTK_DB_PATH", os.path.join(HOME, ".local", "share", "rtk", "history.db"))
RTK_BIN = os.environ.get("RTK_BIN", "rtk")
PORT = int(os.environ.get("PORT", "8095"))
SSE_INTERVAL = int(os.environ.get("SSE_INTERVAL", "2"))
COLLECTOR_INTERVAL = float(os.environ.get("COLLECTOR_INTERVAL", "0.25"))
WEEKLY_CACHE_DIR = os.environ.get("WEEKLY_CACHE_DIR", os.path.join(HOME, ".cache", "claude-tools-dashboard"))

# When Headroom's subscription_window poller hasn't refreshed in this long,
# the Claude Usage card is surfaced as "stale" instead of "ok" so the user
# can tell when upstream credentials expired or the poller stalled, rather
# than staring at a forever-green card frozen on old percentages.
CLAUDE_USAGE_STALE_AFTER_SECONDS = 300


def _utc_now():
    """Return current UTC time. Exposed as a helper so tests can freeze time
    when asserting claude_usage staleness transitions."""
    return datetime.now(timezone.utc)

# Persistent state for sparklines and fallback
_last_good = {}
# Last successful claude_usage fetch. Pinned here so that transient Headroom
# /stats failures (e.g. slow response while Headroom is chewing on a 100k
# request) don't flash the Claude Usage card to "--" between ticks — we keep
# showing the previous numbers until a fresh successful fetch arrives.
_claude_usage_last_good = None
_sparkline_buffers = {
    "rtk": deque(maxlen=240),
    "headroom": deque(maxlen=240),
}
_headroom_last_total = 0
_headroom_history = []
_last_collect_success = {
    "rtk": 0.0,
    "headroom": 0.0,
}

# Cached version strings. resolve_versions_once() populates this at module
# import, and the collectors read from it on every tick to avoid subprocess
# on the hot path.
_cached_versions = {
    "rtk": None,
}


def resolve_versions_once():
    """Resolve tool versions once. Called by DashboardCollector.run() at thread startup.
    Per-tool precedence: TOOL_VERSION env var > binary --version > "unknown"."""
    rtk_v = os.environ.get("RTK_VERSION") or _run([RTK_BIN, "--version"])
    _cached_versions["rtk"] = rtk_v if rtk_v else "unknown"


def _run(cmd, timeout=2):
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


_SECRET_PATTERNS = [
    (re.compile(r'((?:-e|--env)\s+\S*=)\S+'), r'\1***'),
    (re.compile(r'(\b\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD)\s*=)\S+', re.IGNORECASE), r'\1***'),
    (re.compile(r'sk-ant-[A-Za-z0-9_-]{8,}'), 'sk-ant-***'),
    # HTTP Authorization header with scheme (token, Bearer, Basic)
    (re.compile(r'([Aa]uthorization:\s*(?:[Tt]oken|[Bb]earer|[Bb]asic)\s+)\S+'), r'\1***'),
    # Custom X-*-Key or X-*-Token headers (X-API-Key, X-Auth-Token, etc.)
    (re.compile(r'(\b[Xx]-[\w-]*(?:[Kk]ey|[Tt]oken):\s*)\S+'), r'\1***'),
    # curl -u user:password
    (re.compile(r'(\s-u\s+[^\s:]+:)\S+'), r'\1***'),
]


def _sanitise_cmd(cmd):
    """Redact secrets from shell commands before they reach the frontend."""
    if not cmd:
        return cmd
    for pattern, replacement in _SECRET_PATTERNS:
        cmd = pattern.sub(replacement, cmd)
    return cmd


def collect_rtk():
    """Read rtk SQLite DB and return stats + history."""
    try:
        db_path = RTK_DB_PATH
        if not os.path.exists(db_path):
            return None

        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Aggregate stats
        cur.execute(
            "SELECT COUNT(*) as cnt, "
            "COALESCE(SUM(saved_tokens), 0) as total_saved, "
            "COALESCE(AVG(savings_pct), 0) as avg_pct, "
            "COALESCE(SUM(exec_time_ms), 0) as total_time "
            "FROM commands"
        )
        row = cur.fetchone()
        total_commands = row["cnt"]
        total_saved = row["total_saved"]
        avg_savings_pct = round(row["avg_pct"], 1)
        total_time_ms = row["total_time"]

        # Last 100 entries for history
        cur.execute(
            "SELECT timestamp, original_cmd, saved_tokens, savings_pct "
            "FROM commands ORDER BY id DESC LIMIT 100"
        )
        history = []
        for r in cur.fetchall():
            cmd = r["original_cmd"]
            if cmd.startswith("rtk "):
                cmd = cmd[4:]
            cmd = _sanitise_cmd(cmd)
            history.append({
                "time": r["timestamp"],
                "tool": "rtk",
                "cmd": cmd,
                "saved_pct": round(r["savings_pct"], 1),
                "saved_tokens": r["saved_tokens"],
            })

        conn.close()

        # Version (cached at startup)
        version = _cached_versions.get("rtk") or "unknown"

        return {
            "active": True,
            "total_saved": total_saved,
            "total_commands": total_commands,
            "avg_savings_pct": avg_savings_pct,
            "total_time_ms": total_time_ms,
            "version": version or "unknown",
            "history": history,
        }
    except Exception:
        return None


_headroom_version = None


def _system_local_tz():
    """Return the system's local timezone (honors TZ env var in containers)."""
    return datetime.now().astimezone().tzinfo


# Headroom emits some timestamps as naive ISO strings (no tz suffix) even
# though they represent local wall time where the headroom process is running.
# We interpret them in this timezone and convert to explicit UTC so the
# dashboard's string-based sort is chronologically correct and the client
# can display them consistently. Falls back to system local (TZ env var).
_HEADROOM_ASSUMED_TZ = _system_local_tz()


def _normalize_iso_ts(ts, assumed_tz):
    """Return `ts` as an explicit-UTC ISO string.

    - Already has tzinfo → converted to UTC, offset re-emitted as `+00:00`.
    - Naive → treated as wall-clock in `assumed_tz`, then converted to UTC.
    - Unparseable or empty → returned unchanged.
    """
    if not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assumed_tz)
    return dt.astimezone(timezone.utc).isoformat()


def _format_headroom_recent_requests(entries):
    """Convert proxy request logs into dashboard feed rows."""
    history = []
    for entry in entries or []:
        model = entry.get("model") or "unknown"
        original = int(entry.get("input_tokens_original") or 0)
        optimized = int(entry.get("input_tokens_optimized") or 0)
        saved_tokens = int(entry.get("tokens_saved") or 0)
        saved_pct = round(entry.get("savings_percent") or 0, 1)

        if model.startswith("passthrough:"):
            cmd = model.replace("passthrough:", "", 1) + " passthrough"
        elif original > 0 and optimized > 0 and original != optimized:
            cmd = f"{model} {original:,} -> {optimized:,} tokens"
        elif original > 0:
            cmd = f"{model} {original:,} input tokens"
        else:
            cmd = f"{model} request"

        raw_ts = entry.get("timestamp")
        ts = _normalize_iso_ts(raw_ts, _HEADROOM_ASSUMED_TZ) if raw_ts else datetime.now(timezone.utc).isoformat()

        history.append({
            "time": ts,
            "tool": "headroom",
            "cmd": cmd,
            "saved_tokens": saved_tokens,
            "saved_pct": saved_pct,
        })
    return history


def _fetch_headroom_stats_raw():
    """Fetch raw Headroom /stats JSON once. Shared between collect_headroom
    and collect_claude_usage so collect_all only issues one /stats request
    per tick instead of two. Returns None on failure (URLError, timeout,
    malformed JSON) — callers fall back to their own handling."""
    try:
        resp = urlopen(f"{HEADROOM_URL}/stats", timeout=2)
        return json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError):
        return None


def collect_headroom(stats_raw=None):
    """Check headroom proxy stats endpoint.

    When stats_raw is provided (by collect_all sharing a single /stats fetch
    across the tick), parse it directly instead of hitting the endpoint
    again. Otherwise, fall back to the standalone fetch path so tests and
    direct callers keep working.
    """
    try:
        global _headroom_version, _headroom_last_total, _headroom_history
        if _headroom_version is None:
            _headroom_version = os.environ.get("HEADROOM_VERSION")
        if _headroom_version is None:
            try:
                hresp = urlopen(f"{HEADROOM_URL}/health", timeout=2)
                hdata = json.loads(hresp.read().decode())
                v = hdata.get("version")
                if v:
                    _headroom_version = v
            except (URLError, OSError, json.JSONDecodeError):
                pass
        version = _headroom_version or "unknown"

        try:
            raw = stats_raw if stats_raw is not None else _fetch_headroom_stats_raw()
            if raw is None:
                raise URLError("shared /stats fetch returned None")

            tokens_section = raw.get("tokens") or {}
            cache_section = raw.get("compression_cache") or {}
            display = raw.get("display_session") or {}
            persist = (raw.get("persistent_savings") or {}).get("lifetime") or {}
            req_stats = raw.get("requests") or {}
            latency = raw.get("latency") or {}
            prefix_totals = ((raw.get("prefix_cache") or {}).get("totals") or {})
            recent_requests = raw.get("recent_requests") or []

            total_saved = tokens_section.get("saved") or 0
            avg_pct = round(tokens_section.get("savings_percent") or 0, 1)

            if _headroom_last_total > 0 and total_saved > _headroom_last_total:
                delta = total_saved - _headroom_last_total
                if avg_pct and avg_pct > 0:
                    input_est = int(round(delta / (avg_pct / 100)))
                    output_est = max(0, input_est - delta)
                    cmd_text = f"compressed {input_est:,} → {output_est:,} tokens"
                else:
                    cmd_text = "compression event"
                _headroom_history.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "tool": "headroom",
                    "cmd": cmd_text,
                    "saved_tokens": delta,
                    "saved_pct": avg_pct,
                })
                _headroom_history = _headroom_history[-100:]
            _headroom_last_total = total_saved

            # Prefer our own _headroom_history (built from delta-on-total_saved
            # every tick, so it captures every compression event). Fall back to
            # recent_requests only when we haven't accumulated our own history
            # yet -- upstream's recent_requests is a tiny ring buffer that can
            # go stale for long stretches while activity keeps flowing.
            if _headroom_history:
                history = list(_headroom_history)
            else:
                history = _format_headroom_recent_requests(recent_requests)

            return {
                "active": True,
                "version": version,
                "total_saved": total_saved,
                "avg_savings_pct": avg_pct,
                "sessions": cache_section.get("active_sessions", 0),
                "session_saved": display.get("tokens_saved", 0),
                "lifetime_saved": persist.get("tokens_saved", 0),
                "session_saved_usd": display.get("compression_savings_usd", 0),
                "lifetime_saved_usd": persist.get("compression_savings_usd", 0),
                "cache_hit_rate": round(prefix_totals.get("hit_rate") or 0, 1),
                "requests_total": req_stats.get("total") or 0,
                "requests_failed": req_stats.get("failed") or 0,
                "avg_latency_ms": round(latency.get("average_ms") or 0, 1),
                "history": history,
            }
        except (URLError, OSError, json.JSONDecodeError):
            # Return None so collect_all() falls back to _last_good instead of
            # overwriting it with a stub that lacks total_saved/lifetime_saved.
            # A stub would drop headroom's contribution from combined_saved on
            # every transient error, making this_week flap negative.
            return None
    except Exception:
        return None


def collect_claude_usage(stats_raw=None):
    """Pull Claude subscription window values from Headroom's /stats endpoint.

    Headroom already polls https://api.anthropic.com/api/oauth/usage on a
    sane cadence and caches the result under subscription_window.latest in
    /stats. We piggyback on its cache instead of hitting Anthropic directly,
    which means (a) no OAuth token handling here, (b) no per-token rate
    limit to dodge, (c) one source of truth for "what does Anthropic think
    my usage is right now".

    When collect_all passes in stats_raw, reuse that payload instead of
    re-fetching /stats — otherwise we burn 240 extra /stats calls per minute
    at COLLECTOR_INTERVAL=0.25 and compound timeout stalls whenever Headroom
    is slow. Direct callers (tests, cold start) leave stats_raw=None and
    fall back to a standalone fetch.

    Freshness: Headroom's upstream poller can fail silently (expired OAuth
    token, 4xx from Anthropic, network hiccups). When that happens, the
    subscription_window.latest values stay frozen from the last good poll.
    We detect this by comparing latest.polled_at to now; older than
    CLAUDE_USAGE_STALE_AFTER_SECONDS, and the result carries health="stale"
    so the dashboard can stop showing the card as forever-green. Missing
    polled_at (legacy upstream) is also treated as stale — better a false
    positive than a hidden outage.

    On transient fetch failure returns the last successful payload instead
    of {"active": False} so the card doesn't flash to "--" between ticks.
    Cold start with no prior success falls back to {"active": False}.
    """
    global _claude_usage_last_good

    raw = stats_raw if stats_raw is not None else _fetch_headroom_stats_raw()
    if raw is None:
        return _claude_usage_last_good or {"active": False}

    latest = (raw.get("subscription_window") or {}).get("latest") or {}
    if not latest:
        return _claude_usage_last_good or {"active": False}

    five = latest.get("five_hour") or {}
    seven = latest.get("seven_day") or {}
    sonnet = latest.get("seven_day_sonnet") or {}
    extra = latest.get("extra_usage") or {}
    extra_enabled = bool(extra.get("is_enabled"))

    polled_at = latest.get("polled_at")
    health = "stale"
    if polled_at:
        try:
            polled_dt = datetime.fromisoformat(polled_at.replace("Z", "+00:00"))
            if polled_dt.tzinfo is None:
                polled_dt = polled_dt.replace(tzinfo=timezone.utc)
            age = (_utc_now() - polled_dt).total_seconds()
            health = "ok" if age <= CLAUDE_USAGE_STALE_AFTER_SECONDS else "stale"
        except (ValueError, TypeError):
            health = "stale"

    result = {
        "active": True,
        "health": health,
        "polled_at": polled_at,
        "session_pct": five.get("utilization_pct"),
        "session_reset": five.get("resets_at"),
        "weekly_pct": seven.get("utilization_pct"),
        "weekly_reset": seven.get("resets_at"),
        "sonnet_pct": sonnet.get("utilization_pct"),
        "sonnet_reset": sonnet.get("resets_at"),
        "extra_usage_enabled": extra_enabled,
        # Units are USD (used_credits_usd / monthly_limit_usd). Previously
        # when we hit Anthropic direct these were in cents; the frontend's
        # extra_usage card formats them as dollars now.
        "extra_usage_monthly_limit": extra.get("monthly_limit_usd") if extra_enabled else None,
        "extra_usage_used": extra.get("used_credits_usd") if extra_enabled else None,
        "extra_usage_pct": extra.get("utilization_pct") if extra_enabled else None,
    }
    _claude_usage_last_good = result
    return result


WEEKLY_CACHE_SCHEMA_VERSION = 3

# Fingerprint of the combined_saved formula used when a baseline is written.
# Stored inside weekly.json so _load_weekly_cache can detect any future change
# to the formula and drop the baseline instead of comparing incomparable
# totals. Whenever collect_all's combined_saved computation changes, update
# this string — existing caches will auto-rebase on next load.
COMBINED_SAVED_DEFINITION = (
    "headroom:lifetime_saved||total_saved;rtk:total_saved"
)


def _load_weekly_cache():
    """Load weekly savings snapshot from disk.

    Pre-v2: dropped (baseline was written against the old total_saved-based
    combined_saved definition, incomparable to the current lifetime_saved
    formula).

    v2: the formula matches v3, so migrate in place — preserve the baseline,
    stamp the schema version and definition fingerprint forward so the user
    does not lose their weekly progress on upgrade.

    v3+: require an exact combined_saved_definition match. A mismatch means
    the formula changed without a schema bump (or the definition constant
    was edited); drop the cache so collect_all re-seeds a fresh baseline.
    """
    path = os.path.join(WEEKLY_CACHE_DIR, "weekly.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    ver = data.get("schema_version", 1)
    if ver < 2:
        return {}
    if ver == 2:
        # Persist the migration so subsequent loads hit the fast path
        # (v3+ exact-match branch) instead of re-migrating every tick.
        data["schema_version"] = WEEKLY_CACHE_SCHEMA_VERSION
        data["combined_saved_definition"] = COMBINED_SAVED_DEFINITION
        _save_weekly_cache(data)
        return data
    if data.get("combined_saved_definition") != COMBINED_SAVED_DEFINITION:
        return {}
    return data


def _save_weekly_cache(data):
    """Save weekly savings snapshot to disk."""
    os.makedirs(WEEKLY_CACHE_DIR, exist_ok=True)
    path = os.path.join(WEEKLY_CACHE_DIR, "weekly.json")
    payload = dict(data)
    payload["schema_version"] = WEEKLY_CACHE_SCHEMA_VERSION
    payload["combined_saved_definition"] = COMBINED_SAVED_DEFINITION
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def _group_history(entries):
    """Collapse consecutive same-tool, same-prefix entries within 10s."""
    if not entries:
        return entries

    grouped = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        # Never collapse entries that saved >1000 tokens
        if entry.get("saved_tokens", 0) > 1000:
            grouped.append(entry)
            i += 1
            continue

        # Extract command prefix (first word)
        cmd_parts = (entry.get("cmd") or "").split()
        prefix = cmd_parts[0] if cmd_parts else ""
        tool = entry.get("tool", "")
        entry_time = entry.get("time", "")

        # Collect consecutive entries with same tool + prefix within 10s
        batch = [entry]
        j = i + 1
        while j < len(entries):
            next_entry = entries[j]
            # Don't absorb high-savings entries into a group
            if next_entry.get("saved_tokens", 0) > 1000:
                break
            next_parts = (next_entry.get("cmd") or "").split()
            next_prefix = next_parts[0] if next_parts else ""
            next_tool = next_entry.get("tool", "")

            if next_tool != tool or next_prefix != prefix:
                break

            # Check time gap (entries are sorted descending, so earlier entries are later in list)
            try:
                t1 = datetime.fromisoformat(entry_time)
                t2 = datetime.fromisoformat(next_entry.get("time", ""))
                if abs((t1 - t2).total_seconds()) > 10:
                    break
            except (ValueError, TypeError):
                break

            batch.append(next_entry)
            j += 1

        if len(batch) == 1:
            grouped.append(entry)
        else:
            total_saved = sum(e.get("saved_tokens", 0) for e in batch)
            total_pct_weighted = 0
            total_weight = 0
            for e in batch:
                s = e.get("saved_tokens", 0)
                total_pct_weighted += e.get("saved_pct", 0) * max(s, 1)
                total_weight += max(s, 1)
            avg_pct = round(total_pct_weighted / total_weight, 1) if total_weight > 0 else 0

            grouped.append({
                "time": batch[0].get("time"),
                "tool": tool,
                "cmd": f"{len(batch)}x {prefix}",
                "saved_tokens": total_saved,
                "saved_pct": avg_pct,
                "count": len(batch),
                "grouped": True,
            })

        i = j

    return grouped


def _reset_in_future(iso_ts):
    """True iff `iso_ts` is a parseable ISO-8601 timestamp strictly in the future.

    Used by _flatten_snapshot to decide whether a cached pct/reset pair still
    refers to a live reset window. When the Anthropic usage endpoint is rate
    limited, collect_claude_usage() serves its last successful response
    indefinitely — but once the reset time has passed, the utilization value
    refers to a dead window and must be scrubbed before it reaches the UI.
    """
    if not iso_ts:
        return False
    try:
        return datetime.fromisoformat(iso_ts) > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _format_claude_reset(iso_ts, local_tz=None):
    """Render an ISO-8601 reset timestamp as a local-time display string.

    Anthropic returns UTC-aware ISO strings. The dashboard must display them
    in the user's local timezone, otherwise "Tue 18:00 UTC" looks like a
    different wall-clock time than the official "Tue 7:00 PM" on claude.ai.

    `local_tz` defaults to None, which uses the process's system local tz
    (honors the TZ env var in containers). Tests can pass an explicit
    zoneinfo.ZoneInfo to avoid depending on host tz configuration.
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if local_tz is not None:
            dt = dt.astimezone(local_tz)
        else:
            dt = dt.astimezone()
        return dt.strftime("%a %-d %b %H:%M")
    except (ValueError, TypeError):
        return ""


def _same_reset_window(a, b, tolerance_seconds=5):
    """Return True iff two ISO-8601 resets_at timestamps refer to the same reset window.

    Anthropic's usage API returns resets_at with sub-second precision that
    jitters (observed drift up to ~1s) between calls even within the same
    week. Compare with an absolute time tolerance so boundary drift (e.g.
    17:59:59.8 vs 18:00:00.2, which straddles a minute boundary) does not
    trigger false rotations. A real weekly reset advances the timestamp by
    7 days, well outside any tolerance this function would use.
    """
    if not a or not b:
        return False
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
        return abs((da - db).total_seconds()) <= tolerance_seconds
    except (ValueError, TypeError):
        return a == b


def _flatten_snapshot(snap):
    """Flatten the collector snapshot to a status-line-friendly dict.

    Contract pinned by tests in tests/test_app.py. Returns a stable
    45-key shape whether the collector has ticked (snap is a dict) or
    not (snap is None), and defensively defaults every sub-object so
    a partial snapshot never raises KeyError.

    Numeric fields default to 0. Booleans default to False. Health
    defaults to "error", version to "unknown", freshness_label to
    "idle". Claude usage fields default to None (not zero) so the
    status line can distinguish "API not fetched" from "zero
    utilisation".
    """
    ready = snap is not None
    snap = snap or {}

    claude = snap.get("claude_usage") or {}
    weekly = snap.get("weekly") or {}
    sparklines = snap.get("sparklines") or {}

    rtk = snap.get("rtk") or {}
    headroom = snap.get("headroom") or {}

    spark_rtk = sparklines.get("rtk") or {}
    spark_hr = sparklines.get("headroom") or {}

    claude_active = bool(claude.get("active"))
    # claude_usage_health lets statusline consumers distinguish "no data ever
    # seen" (error) from "stale data held open by fallback" (stale) from
    # "fresh" (ok). Legacy shapes without a health field default to ok iff
    # active, else error — matches the pre-freshness contract.
    claude_usage_health = claude.get("health") or (
        "ok" if claude_active else "error"
    )
    claude_usage_polled_at = claude.get("polled_at")
    # A cached pct only makes sense while its reset window is still in the
    # future. Once the window rolls over, the cached utilization refers to
    # a dead window and must be scrubbed — otherwise a rate-limited backend
    # keeps showing ghost percentages (e.g. 20% from a 5-hour session that
    # already reset two hours ago).
    session_valid = claude_active and _reset_in_future(claude.get("session_reset"))
    weekly_valid = claude_active and _reset_in_future(claude.get("weekly_reset"))
    sonnet_valid = claude_active and _reset_in_future(claude.get("sonnet_reset"))

    def _claude(key):
        return claude.get(key) if claude_active else None

    hr_lifetime = headroom.get("lifetime_saved") or 0
    hr_lifetime_usd = headroom.get("lifetime_saved_usd") or 0
    usd_per_token = (
        (hr_lifetime_usd / hr_lifetime)
        if hr_lifetime > 0 and hr_lifetime_usd > 0
        else None
    )
    combined_saved_usd = None
    if usd_per_token is not None:
        non_headroom_tokens = rtk.get("total_saved") or 0
        combined_saved_usd = hr_lifetime_usd + non_headroom_tokens * usd_per_token

    return {
        "ready": ready,
        "timestamp": snap.get("timestamp"),

        "claude_active": claude_active,
        "session_pct": _claude("session_pct") if session_valid else None,
        "session_reset": _claude("session_reset") if session_valid else None,
        "weekly_pct": _claude("weekly_pct") if weekly_valid else None,
        "weekly_reset": _claude("weekly_reset") if weekly_valid else None,
        # weekly_reset_display is derived from the same stale cache as
        # weekly_reset, so scrub it alongside weekly when the window is dead.
        # When claude_active is False, the display string reflects an earlier
        # successful fetch and is still the best info we have — pass it through.
        "weekly_reset_display": (
            None
            if (claude_active and not weekly_valid)
            else (weekly.get("reset_display") or None)
        ),
        "sonnet_pct": _claude("sonnet_pct") if sonnet_valid else None,
        "sonnet_reset": _claude("sonnet_reset") if sonnet_valid else None,

        "combined_saved": snap.get("combined_saved", 0),
        "combined_saved_usd": combined_saved_usd,
        "this_week_saved": weekly.get("this_week", 0),
        "last_week_saved": weekly.get("last_week", 0),
        "burn_rate_daily": weekly.get("burn_rate_daily", 0),
        "week_is_fresh": weekly.get("week_is_fresh", False),

        "rtk_active": rtk.get("active", False),
        "rtk_health": rtk.get("health", "error"),
        "rtk_version": rtk.get("version", "unknown"),
        "rtk_saved": rtk.get("total_saved", 0),
        "rtk_delta": spark_rtk.get("delta", 0),
        "rtk_commands": rtk.get("total_commands", 0),
        "rtk_avg_pct": rtk.get("avg_savings_pct", 0),

        "headroom_active": headroom.get("active", False),
        "headroom_health": headroom.get("health", "error"),
        "headroom_version": headroom.get("version", "unknown"),
        "headroom_saved": headroom.get("total_saved", 0),
        "headroom_delta": spark_hr.get("delta", 0),
        "headroom_sessions": headroom.get("sessions", 0),
        "headroom_session_saved": headroom.get("session_saved", 0),
        "headroom_lifetime_saved": headroom.get("lifetime_saved", 0),
        "headroom_session_saved_usd": headroom.get("session_saved_usd", 0),
        "headroom_lifetime_saved_usd": headroom.get("lifetime_saved_usd", 0),
        "headroom_cache_hit_rate": headroom.get("cache_hit_rate", 0),
        "headroom_requests_total": headroom.get("requests_total", 0),
        "headroom_requests_failed": headroom.get("requests_failed", 0),
        "headroom_avg_latency_ms": headroom.get("avg_latency_ms", 0),

        "extra_usage_enabled": bool(_claude("extra_usage_enabled")),
        "extra_usage_monthly_limit": _claude("extra_usage_monthly_limit"),
        "extra_usage_used": _claude("extra_usage_used"),
        "extra_usage_pct": _claude("extra_usage_pct"),

        "claude_usage_health": claude_usage_health,
        "claude_usage_polled_at": claude_usage_polled_at,
    }


def collect_all():
    """Collect from all tools, maintain sparklines and fallbacks."""
    global _last_good

    # Fetch Headroom /stats once per tick and share the payload with both
    # collect_headroom (savings metrics) and collect_claude_usage
    # (subscription_window poll). Pre-refactor each collector fetched its
    # own copy, doubling the request rate and compounding stalls when
    # Headroom was slow. None on failure — each collector has its own
    # fallback behaviour in that case.
    headroom_stats_raw = _fetch_headroom_stats_raw()

    collectors = {
        "rtk": collect_rtk,
        "headroom": lambda: collect_headroom(stats_raw=headroom_stats_raw),
    }

    results = {}
    for name, fn in collectors.items():
        data = fn()
        if data is not None:
            results[name] = data
            _last_good[name] = data
            _last_collect_success[name] = time.time()
        else:
            results[name] = _last_good.get(name, {"active": False, "version": "unknown", "total_saved": 0, "history": []})

    now_ts = time.time()
    for name in collectors:
        last_ok = _last_collect_success.get(name, 0)
        if results[name].get("active") and last_ok > 0 and (now_ts - last_ok) < 60:
            results[name]["health"] = "ok"
        elif last_ok > 0:
            results[name]["health"] = "stale"
        else:
            results[name]["health"] = "error"

    # Build combined saved total.
    # For headroom, prefer lifetime_saved (persistent across process restarts)
    # over total_saved (which is the in-memory stats-cycle counter — underreported).
    combined_saved = 0
    for name in collectors:
        tool_data = results[name]
        if name == "headroom":
            combined_saved += tool_data.get("lifetime_saved") or tool_data.get("total_saved", 0)
        else:
            combined_saved += tool_data.get("total_saved", 0)

    # Weekly savings tracking — reuse the same /stats payload to avoid a
    # second HTTP call per tick (Codex P1).
    claude_usage = collect_claude_usage(stats_raw=headroom_stats_raw)
    weekly_data = _load_weekly_cache()

    if claude_usage and claude_usage.get("weekly_reset"):
        fresh_reset = claude_usage["weekly_reset"]
        stored_reset = weekly_data.get("weekly_reset_at", "")

        # Reset has moved forward -- rotate weeks
        if not _same_reset_window(fresh_reset, stored_reset) and stored_reset:
            baseline = weekly_data.get("current_week_baseline", 0)
            weekly_data["last_week_savings"] = combined_saved - baseline
            weekly_data["last_week_end"] = stored_reset
            weekly_data["current_week_baseline"] = combined_saved
            weekly_data["current_week_start"] = stored_reset
            weekly_data["weekly_reset_at"] = fresh_reset
            _save_weekly_cache(weekly_data)
        elif not stored_reset:
            # First run -- set baseline
            weekly_data["current_week_baseline"] = combined_saved
            weekly_data["current_week_start"] = datetime.now(timezone.utc).isoformat()
            weekly_data["weekly_reset_at"] = fresh_reset
            weekly_data["last_week_savings"] = 0
            _save_weekly_cache(weekly_data)

    this_week_savings = combined_saved - weekly_data.get("current_week_baseline", combined_saved)
    last_week_savings = weekly_data.get("last_week_savings", 0)

    # Burn rate: savings per day this week
    week_start = weekly_data.get("current_week_start")
    if week_start:
        try:
            start_dt = datetime.fromisoformat(week_start)
            elapsed_days = max(1.0, (datetime.now(timezone.utc) - start_dt).total_seconds() / 86400)
            burn_rate_daily = int(this_week_savings / elapsed_days)
        except (ValueError, TypeError):
            burn_rate_daily = 0
    else:
        burn_rate_daily = 0

    # Flag fresh tracker (started <1hr ago with 0 savings)
    week_is_fresh = False
    if this_week_savings == 0 and week_start:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(week_start)).total_seconds()
            week_is_fresh = elapsed < 3600
        except (ValueError, TypeError):
            pass

    # Format reset display in the user's local timezone (e.g. "Thu 3 Apr 15:00")
    reset_display = _format_claude_reset(
        claude_usage.get("weekly_reset") if claude_usage else None
    )

    # Update sparkline buffers with cumulative totals.
    # For headroom, feed on lifetime_saved (persistent across container
    # restarts) instead of total_saved (in-memory stats-cycle counter that
    # rewinds to zero on restart). Mixing the two counters produced a huge
    # negative delta whenever Headroom restarted, flattening the spark line.
    # Align the buffer with the headline combined_saved formula.
    now = time.time()
    for name in collectors:
        tool_data = results[name]
        if not tool_data.get("active"):
            continue
        if name == "headroom":
            cumulative = (
                tool_data.get("lifetime_saved")
                or tool_data.get("total_saved", 0)
            )
        else:
            cumulative = tool_data.get("total_saved", 0)
        _sparkline_buffers[name].append((now, cumulative))

    # Compute deltas and sparkline points
    sparklines = {}
    for name in collectors:
        buf = _sparkline_buffers[name]
        entries = list(buf)

        # Delta: diff of last 2 entries
        if len(entries) >= 2:
            delta = entries[-1][1] - entries[-2][1]
        else:
            delta = 0

        # Sparkline points: diffs of consecutive entries
        points = []
        for i in range(1, len(entries)):
            points.append(entries[i][1] - entries[i - 1][1])

        sparklines[name] = {
            "delta": delta,
            "points": points,
        }

    # Merge history from all tools that have it
    history = []
    if "history" in results.get("rtk", {}):
        history.extend(results["rtk"]["history"])
    if "history" in results.get("headroom", {}):
        history.extend(results["headroom"]["history"])

    # Sort by time descending, collapse bursts, limit to 50
    history.sort(key=lambda x: x.get("time", ""), reverse=True)
    history = _group_history(history)
    history = history[:50]

    # Per-tool history lists are merged into the top-level `history` above,
    # so drop them to shave ~15KB per SSE tick. Build a new dict per tool
    # rather than popping in-place — the same tool dict is stored in
    # _last_good by reference, and mutating it would strip history from the
    # fallback cache and empty the feed on the next transient failure.
    for tool_name in ("rtk", "headroom"):
        trimmed = dict(results[tool_name])
        trimmed.pop("history", None)
        results[tool_name] = trimmed

    timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "ready": True,
        "timestamp": timestamp,
        "combined_saved": combined_saved,
        "rtk": results["rtk"],
        "headroom": results["headroom"],
        "sparklines": sparklines,
        "history": history,
        "claude_usage": claude_usage or {"active": False},
        "weekly": {
            "this_week": this_week_savings,
            "last_week": last_week_savings,
            "burn_rate_daily": burn_rate_daily,
            "reset_display": reset_display,
            "week_is_fresh": week_is_fresh,
        },
    }


class DashboardCollector(threading.Thread):
    """Background daemon that ticks COLLECTOR_INTERVAL seconds.

    On each tick: runs the four tool collectors via collect_all() and
    stores the resulting payload as a shared snapshot under a lock.
    Version strings are resolved once at startup and cached in
    _cached_versions so the hot path does no subprocess work.
    """

    def __init__(self):
        super().__init__(daemon=True, name="DashboardCollector")
        self._lock = threading.Lock()
        self._snapshot = None

    def run(self):
        try:
            resolve_versions_once()
        except Exception as exc:
            print(f"[collector] version resolution failed: {exc}", flush=True)

        while True:
            try:
                payload = collect_all()
                with self._lock:
                    self._snapshot = payload
            except Exception as exc:
                # Never die; next tick will retry. Print for journalctl.
                print(f"[collector] tick failed: {exc}", flush=True)
            time.sleep(COLLECTOR_INTERVAL)

    def snapshot(self):
        """Return the most recent snapshot (or None if not yet collected)."""
        with self._lock:
            return self._snapshot


_collector = DashboardCollector()
_collector.start()


# --- HTML Frontend ---

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Tools Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; overflow-y: auto; }
body {
    background: #0a0a1a;
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Courier New', monospace;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    color: #ccc;
    padding: 24px;
    display: flex;
    flex-direction: column;
}

/* Header */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid #1a1a2e;
    margin-bottom: 20px;
    padding-bottom: 12px;
}
.header-left {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
}
.header-left:hover .header-title {
    text-shadow: 0 0 6px #00ff88;
}
.pulse-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #00ff88;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 4px #00ff88; }
    50% { opacity: 0.4; box-shadow: 0 0 1px #00ff88; }
}
.header-title {
    color: #00ff88;
    font-size: 16px;
    letter-spacing: 3px;
    font-weight: bold;
}
.header-right {
    color: #666;
    font-size: 13px;
}

/* Cards grid */
.cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 12px;
}
.card {
    background: #111;
    border: 1px solid #1a1a2e;
    padding: 18px;
    border-radius: 4px;
    transition: opacity 0.3s;
}
.card.inactive { opacity: 0.5; }
.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    position: relative;
}
.card-name {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 2px;
    font-weight: bold;
    color: #999;
    text-decoration: none;
}
.card-name:hover { color: #fff; }
.card-version {
    color: #555;
    font-size: 11px;
}
.card-value {
    font-size: 36px;
    font-weight: bold;
    margin-bottom: 2px;
}
.card-sub {
    font-size: 13px;
    color: #777;
    margin-bottom: 12px;
}
.progress-track {
    height: 3px;
    background: #0a0a0a;
    border-radius: 2px;
    margin-bottom: 12px;
}
.progress-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.6s ease;
}
.card-stats {
    display: flex;
    gap: 16px;
    font-size: 12px;
    margin-bottom: 10px;
}
.card-stats .label { color: #888; }
.card-stats .val { color: #aaa; }
.sparkline-container { margin-bottom: 6px; }
.sparkline-container svg { width: 100%; height: 35px; }
.card-delta {
    font-size: 12px;
    color: #555;
}
.card-delta.active {
    color: #00ff88;
    animation: flash 0.5s;
}
@keyframes flash {
    0% { opacity: 0.3; }
    100% { opacity: 1; }
}

/* Combined + Usage card specifics */
.cards .card-combined .card-value { color: #00ff88; }
.cards .card-combined .card-stats { gap: 12px; }
.cards .card-combined .card-stats > span { white-space: nowrap; }

.cards .card-usage .usage-toggle {
    display: flex;
    gap: 0;
    background: #0a0a0a;
    border: 1px solid #1a1a2e;
    border-radius: 3px;
    padding: 2px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.cards .card-usage .usage-toggle button {
    background: transparent;
    border: none;
    color: #666;
    padding: 3px 8px;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    border-radius: 2px;
}
.cards .card-usage .usage-toggle button.active {
    background: #1a1a2e;
    color: #fff;
}
.cards .card-usage .usage-toggle button:disabled {
    color: #333;
    cursor: not-allowed;
}
.cards .card-usage .usage-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 4px 0;
    font-size: 13px;
    color: #aaa;
}
.cards .card-usage .usage-row .label { color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
.cards .card-usage .usage-row .val { color: #fff; font-weight: 600; }
.cards .card-usage .usage-row .val.pct-green { color: #00ff88; }
.cards .card-usage .usage-row .val.pct-yellow { color: #ffcc00; }
.cards .card-usage .usage-row .val.pct-red { color: #ff4444; }
.cards .card-usage .progress-track { margin-top: 12px; }
.cards .card-usage .progress-fill.pct-green { background: #00ff88; }
.cards .card-usage .progress-fill.pct-yellow { background: #ffcc00; }
.cards .card-usage .progress-fill.pct-red { background: #ff4444; }
.cards .card-usage .val-time { color: #bbb; font-weight: 600; }

/* Tool accent colours — purely for card identity, no semantic meaning */
.clr-rtk { color: #3b82f6; }
.fill-rtk { background: #3b82f6; }
.stroke-rtk { stroke: #3b82f6; }
.area-rtk { fill: rgba(59, 130, 246, 0.1); }

.clr-headroom { color: #8b5cf6; }
.fill-headroom { background: #8b5cf6; }
.stroke-headroom { stroke: #8b5cf6; }
.area-headroom { fill: rgba(139, 92, 246, 0.1); }

.health-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    position: absolute;
    left: -14px;
    top: 50%;
    transform: translateY(-50%);
}
.health-ok {
    background: #00ff88;
    animation: pulse 2s infinite;
}
.health-stale {
    background: #ffcc00;
}
.health-error {
    background: #ff4444;
}

/* Feed */
.feed-container {
    background: #111;
    border: 1px solid #1a1a2e;
    border-radius: 4px;
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
}
.feed-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid #1a1a2e;
}
.feed-title {
    color: #999;
    font-size: 12px;
    text-transform: uppercase;
    font-weight: bold;
    letter-spacing: 2px;
}
.feed-count {
    color: #555;
    font-size: 12px;
}
.feed-area {
    flex: 1;
    overflow-y: auto;
    scrollbar-width: thin;          /* Firefox */
    scrollbar-color: #2a2a2a #0a0a0a;
    min-height: 0;
    font-size: 13px;
    line-height: 2.1;
    padding: 10px 16px;
}
.feed-area::-webkit-scrollbar {
    width: 6px;
}
.feed-area::-webkit-scrollbar-track {
    background: #0a0a0a;
}
.feed-area::-webkit-scrollbar-thumb {
    background: #2a2a2a;
    border-radius: 3px;
}
.feed-area::-webkit-scrollbar-thumb:hover {
    background: #3a3a3a;
}
.feed-line {
    display: flex;
    gap: 16px;
    align-items: center;
    border-left: 2px solid transparent;
    padding-left: 14px;
}
.feed-time {
    color: #666;
    min-width: 70px;
    flex-shrink: 0;
}
.feed-tool {
    font-weight: bold;
    min-width: 95px;
    flex-shrink: 0;
}
.feed-cmd {
    color: #999;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.feed-savings {
    min-width: 160px;
    text-align: right;
    flex-shrink: 0;
}
.feed-savings.positive { color: #00ff88; }
.feed-savings.zero { color: #444; }
.feed-savings.info { color: #888; }
.feed-line.muted {
    font-size: 12px;
    opacity: 0.6;
}
.feed-line.highlight {
    border-left-color: currentColor;
}
.feed-line.grouped {
    border-left-color: currentColor;
    font-style: italic;
}

@media (max-width: 1000px) {
    .cards { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 550px) {
    .cards { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<!-- Header -->
<div class="header">
    <a class="header-left" href="https://github.com/Will-Luck/claude-tools-dashboard" target="_blank" rel="noopener noreferrer" title="View source on GitHub">
        <div class="pulse-dot"></div>
        <div class="header-title">CLAUDE TOOLS</div>
    </a>
    <div class="header-right" id="clock">--:--:-- &blacksquare; -- --- ----</div>
</div>

<!-- Cards -->
<div class="cards">
    <!-- Combined -->
    <div class="card card-combined" id="summary-combined">
        <div class="card-header">
            <span class="health-dot health-ok" id="summary-combined-health"></span>
            <span class="card-name">Combined</span>
        </div>
        <div class="card-value" id="summary-combined-value">--</div>
        <div class="card-sub" id="summary-combined-sub">tokens saved</div>
        <div class="card-stats" id="summary-combined-stats">
            <span><span class="label">this week</span> <span class="val" id="summary-this-week">--</span></span>
            <span><span class="label">last week</span> <span class="val" id="summary-last-week">--</span></span>
            <span><span class="label">avg/day</span> <span class="val" id="summary-burn">--</span></span>
        </div>
    </div>

    <!-- RTK -->
    <div class="card" id="rtk-card">
        <div class="card-header">
            <span class="health-dot health-error" id="rtk-health"></span><a href="https://github.com/rtk-ai/rtk" target="_blank" class="card-name">RTK</a>
            <span class="card-version" id="rtk-version">--</span>
        </div>
        <div class="card-value clr-rtk" id="rtk-value">--</div>
        <div class="card-sub" id="rtk-sub">tokens saved</div>
        <div class="progress-track"><div class="progress-fill fill-rtk" id="rtk-bar" style="width:0%"></div></div>
        <div class="card-stats" id="rtk-stats">
            <span><span class="label">efficiency</span> <span class="val">--%</span></span>
        </div>
        <div class="sparkline-container"><svg id="rtk-sparkline" viewBox="0 0 200 35" preserveAspectRatio="none"></svg></div>
        <div class="card-delta" id="rtk-delta"></div>
    </div>

    <!-- Headroom -->
    <div class="card" id="headroom-card">
        <div class="card-header">
            <span class="health-dot health-error" id="headroom-health"></span><a href="https://github.com/chopratejas/headroom" target="_blank" class="card-name">Headroom</a>
            <span class="card-version" id="headroom-version">--</span>
        </div>
        <div class="card-value clr-headroom" id="headroom-value">--</div>
        <div class="card-sub" id="headroom-sub">awaiting first session</div>
        <div class="progress-track"><div class="progress-fill fill-headroom" id="headroom-bar" style="width:0%"></div></div>
        <div class="card-stats" id="headroom-stats">
            <span><span class="label">proxy not active</span></span>
        </div>
        <div class="sparkline-container"><svg id="headroom-sparkline" viewBox="0 0 200 35" preserveAspectRatio="none"></svg></div>
        <div class="card-delta" id="headroom-delta"></div>
    </div>

    <!-- Usage (merged Claude / Extra with toggle) -->
    <div class="card card-usage" id="summary-usage">
        <div class="card-header">
            <span class="health-dot health-error" id="summary-usage-health"></span>
            <span class="card-name">Usage</span>
            <div class="usage-toggle" id="summary-usage-toggle">
                <button type="button" data-mode="claude" class="active">Claude</button>
                <button type="button" data-mode="extra">Extra</button>
            </div>
        </div>
        <div id="summary-usage-claude">
            <div class="usage-row"><span class="label">5-Hour</span><span class="val" id="summary-session-pct">--</span></div>
            <div class="usage-row"><span class="label">Weekly</span><span class="val" id="summary-weekly-pct">--</span></div>
            <div class="usage-row"><span class="label">Sonnet</span><span class="val" id="summary-sonnet-pct">--</span></div>
            <div class="usage-row"><span class="label">Reset</span><span class="val val-time" id="summary-claude-reset">--</span></div>
        </div>
        <div id="summary-usage-extra" style="display:none;">
            <div class="card-value dim" id="summary-extra-value">n/a</div>
            <div class="card-sub" id="summary-extra-detail">not enabled</div>
            <div class="progress-track"><div class="progress-fill" id="summary-extra-bar" style="width:0%"></div></div>
        </div>
    </div>
</div>

<!-- Activity Feed -->
<div class="feed-container">
    <div class="feed-header">
        <span class="feed-title">LIVE ACTIVITY</span>
        <span class="feed-count">showing last 0</span>
    </div>
    <div class="feed-area" id="feed"></div>
</div>

<script>
function formatTokens(n, withUnit) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    if (withUnit) return n + ' tokens';
    return String(n);
}

function formatTime(ms) {
    var s = ms / 1000;
    if (s >= 60) return (s / 60).toFixed(1) + 'min';
    return s.toFixed(1) + 's';
}

function shortTime(t) {
    if (!t) return '';
    // Parse as Date so explicit-UTC timestamps (rtk: +00:00) convert to the
    // browser's local time. Naive date-time strings parse as local per
    // ECMAScript, which matches the server-side normalizer that emits
    // headroom's timestamps with explicit UTC offsets.
    var d = new Date(t);
    if (!isNaN(d.getTime())) {
        var h = d.getHours(), mi = d.getMinutes(), s = d.getSeconds();
        return (h < 10 ? '0' : '') + h + ':' +
               (mi < 10 ? '0' : '') + mi + ':' +
               (s < 10 ? '0' : '') + s;
    }
    // Fallback: extract HH:MM:SS for inputs we couldn't parse.
    var m = t.match(/T?(\\d{2}:\\d{2}:\\d{2})/);
    if (m) return m[1];
    return t.length > 8 ? t.substring(0, 8) : t;
}

function shortVersion(v) {
    if (!v || v === 'unknown') return '--';
    // "rtk 0.34.1" -> "0.34.1", "headroom, version 0.5.9" -> "0.5.9"
    var clean = v.replace(/,/g, '').trim();
    var parts = clean.split(' ');
    return parts[parts.length - 1];
}

function pctClass(n) {
    if (n == null) return '';
    if (n > 80) return 'pct-red';
    if (n > 50) return 'pct-yellow';
    return 'pct-green';
}

function applyPctField(el, val) {
    if (val == null) {
        el.textContent = '--';
        el.className = 'val';
    } else {
        el.textContent = val + '%';
        el.className = 'val ' + pctClass(val);
    }
}

var TOOLS = ['rtk', 'headroom'];
var TOOL_COLOURS = {
    rtk: '#3b82f6',
    headroom: '#8b5cf6'
};

// Usage card toggle state. userOverride is null until the user clicks a
// segment; after that it sticks to "claude" or "extra" for the session and
// the SSE tick's auto-default is ignored. See spec: docs/superpowers/specs/
// 2026-04-15-drop-jcode-jdoc-merge-usage-design.md
var usageUserOverride = null;

function wireUsageToggle() {
    var toggle = document.getElementById('summary-usage-toggle');
    if (!toggle) return;
    var buttons = toggle.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener('click', function(e) {
            var btn = e.currentTarget;
            if (btn.disabled) return;
            usageUserOverride = btn.getAttribute('data-mode');
            applyUsageMode(usageUserOverride);
        });
    }
}

function applyUsageMode(mode) {
    var claudeBody = document.getElementById('summary-usage-claude');
    var extraBody = document.getElementById('summary-usage-extra');
    var buttons = document.querySelectorAll('#summary-usage-toggle button');
    for (var i = 0; i < buttons.length; i++) {
        buttons[i].className = buttons[i].getAttribute('data-mode') === mode ? 'active' : '';
    }
    if (mode === 'extra') {
        claudeBody.style.display = 'none';
        extraBody.style.display = '';
    } else {
        claudeBody.style.display = '';
        extraBody.style.display = 'none';
    }
}

function renderSparkline(svgEl, points, tool) {
    if (!points || points.length < 2) {
        svgEl.innerHTML = '';
        return;
    }
    var max = Math.max.apply(null, points);
    if (max === 0) { svgEl.innerHTML = ''; return; }

    var w = 200, hMin = 5, hMax = 30;
    var stepX = w / (points.length - 1);
    var pathParts = [];
    var areaParts = [];

    for (var i = 0; i < points.length; i++) {
        var x = (i * stepX).toFixed(1);
        var y = (hMax - (points[i] / max) * (hMax - hMin)).toFixed(1);
        pathParts.push((i === 0 ? 'M' : 'L') + x + ',' + y);
        areaParts.push((i === 0 ? 'M' : 'L') + x + ',' + y);
    }

    var lastX = ((points.length - 1) * stepX).toFixed(1);
    areaParts.push('L' + lastX + ',35');
    areaParts.push('L0,35');
    areaParts.push('Z');

    svgEl.innerHTML =
        '<path d="' + areaParts.join(' ') + '" class="area-' + tool + '" stroke="none"/>' +
        '<path d="' + pathParts.join(' ') + '" class="stroke-' + tool + '" fill="none" stroke-width="1.5"/>';
}

function updateDashboard(d) {
    var w = d.weekly || {};
    var cu = d.claude_usage || {};
    var rtk = d.rtk || {};
    var hr = d.headroom || {};

    var hrLifetimeTokens = hr.lifetime_saved || 0;
    var hrLifetimeUsd = hr.lifetime_saved_usd || 0;
    var hrDisplayTokens = hrLifetimeTokens || hr.total_saved || 0;
    var usdPerToken = (hrLifetimeTokens > 0 && hrLifetimeUsd > 0) ? hrLifetimeUsd / hrLifetimeTokens : null;
    function tokensToUsd(n) { return usdPerToken != null ? n * usdPerToken : null; }
    function tokensSavedSub(usd) {
        return usd != null ? 'tokens saved · $' + usd.toFixed(2) : 'tokens saved';
    }

    // --- Combined card ---
    document.getElementById('summary-combined-health').className = 'health-dot ' + (d.ready ? 'health-ok' : 'health-error');
    document.getElementById('summary-combined-value').textContent = formatTokens(d.combined_saved || 0);
    var combinedUsd = null;
    if (usdPerToken != null) {
        var nonHrTokens = rtk.total_saved || 0;
        combinedUsd = hrLifetimeUsd + nonHrTokens * usdPerToken;
    }
    document.getElementById('summary-combined-sub').textContent = tokensSavedSub(combinedUsd);
    document.getElementById('summary-this-week').textContent = w.week_is_fresh ? '--' : (w.this_week != null ? formatTokens(w.this_week, true) : '--');
    document.getElementById('summary-last-week').textContent = w.last_week != null ? (w.last_week === 0 ? '0' : formatTokens(w.last_week, true)) : '--';
    document.getElementById('summary-burn').textContent = w.burn_rate_daily != null ? (w.burn_rate_daily === 0 ? '0' : formatTokens(w.burn_rate_daily, true)) : '--';

    // --- Usage card (merged Claude + Extra with toggle) ---
    var cuHealth = cu.health || (cu.active ? 'ok' : 'error');
    document.getElementById('summary-usage-health').className = 'health-dot health-' + cuHealth;

    // Claude sub-view
    applyPctField(document.getElementById('summary-session-pct'), cu.active ? cu.session_pct : null);
    applyPctField(document.getElementById('summary-weekly-pct'), cu.active ? cu.weekly_pct : null);
    applyPctField(document.getElementById('summary-sonnet-pct'), cu.active ? cu.sonnet_pct : null);
    document.getElementById('summary-claude-reset').textContent = w.reset_display || '--';

    // Extra sub-view
    var extraEnabled = !!(cu.active && cu.extra_usage_enabled);
    var extraVal = document.getElementById('summary-extra-value');
    var extraDetail = document.getElementById('summary-extra-detail');
    var extraBar = document.getElementById('summary-extra-bar');
    if (extraEnabled) {
        var extraPct = cu.extra_usage_pct || 0;
        extraVal.className = 'card-value ' + pctClass(extraPct);
        extraVal.textContent = (cu.extra_usage_pct != null ? cu.extra_usage_pct.toFixed(1) : '0') + '%';
        var used = cu.extra_usage_used;
        var limit = cu.extra_usage_monthly_limit;
        extraDetail.textContent = (used != null && limit != null)
            ? '$' + used.toFixed(2) + ' / $' + limit.toFixed(2)
            : 'active';
        extraBar.style.width = Math.min(100, Math.max(0, extraPct)) + '%';
        extraBar.className = 'progress-fill ' + pctClass(extraPct);
    } else {
        extraVal.className = 'card-value dim';
        extraVal.textContent = 'n/a';
        extraDetail.textContent = 'not enabled';
        extraBar.style.width = '0%';
        extraBar.className = 'progress-fill';
    }

    // Disable the Extra segment when extra is off; if the user had
    // previously overridden to "extra", force-clear the override so the
    // card falls back to Claude.
    var extraBtn = document.querySelector('#summary-usage-toggle button[data-mode="extra"]');
    if (extraBtn) extraBtn.disabled = !extraEnabled;
    if (!extraEnabled && usageUserOverride === 'extra') usageUserOverride = null;

    // Default mode auto-follows extra_usage_enabled until the user clicks.
    var defaultMode = extraEnabled ? 'extra' : 'claude';
    applyUsageMode(usageUserOverride || defaultMode);

    // RTK
    var rtkCard = document.getElementById('rtk-card');
    rtkCard.className = rtk.active ? 'card' : 'card inactive';
    var rtkHealth = rtk.health || 'error';
    var rtkDot = document.getElementById('rtk-health');
    if (rtkDot) rtkDot.className = 'health-dot health-' + rtkHealth;
    document.getElementById('rtk-version').textContent = shortVersion(rtk.version);
    document.getElementById('rtk-value').textContent = rtk.active ? formatTokens(rtk.total_saved || 0) : '--';
    document.getElementById('rtk-sub').textContent = tokensSavedSub(rtk.active ? tokensToUsd(rtk.total_saved || 0) : null);
    document.getElementById('rtk-bar').style.width = (rtk.avg_savings_pct || 0) + '%';
    if (rtk.active && rtk.total_commands) {
        var avgMs = rtk.total_time_ms / rtk.total_commands;
        document.getElementById('rtk-stats').innerHTML =
            '<span><span class="label">efficiency</span> <span class="val">' + (rtk.avg_savings_pct || 0) + '%</span></span>' +
            '<span><span class="label">cmds</span> <span class="val">' + rtk.total_commands + '</span></span>' +
            '<span><span class="label">avg</span> <span class="val">' + formatTime(avgMs) + '</span></span>';
    }

    // Headroom
    var hrCard = document.getElementById('headroom-card');
    hrCard.className = hr.active ? 'card' : 'card inactive';
    var hrHealth = hr.health || 'error';
    var hrDot = document.getElementById('headroom-health');
    if (hrDot) hrDot.className = 'health-dot health-' + hrHealth;
    document.getElementById('headroom-version').textContent = shortVersion(hr.version);
    if (hr.active) {
        document.getElementById('headroom-value').textContent = formatTokens(hrDisplayTokens);
        document.getElementById('headroom-sub').textContent = tokensSavedSub(usdPerToken != null ? hrLifetimeUsd : null);
        document.getElementById('headroom-bar').style.width = (hr.avg_savings_pct || 0) + '%';
        document.getElementById('headroom-stats').innerHTML =
            '<span><span class="label">efficiency</span> <span class="val">' + (hr.avg_savings_pct || 0) + '%</span></span>' +
            '<span><span class="label">cache</span> <span class="val">' + Math.round(hr.cache_hit_rate || 0) + '%</span></span>' +
            '<span><span class="label">req</span> <span class="val">' + (hr.requests_total || 0) + '</span></span>';
    } else {
        document.getElementById('headroom-value').textContent = '--';
        document.getElementById('headroom-sub').textContent = 'awaiting first session';
        document.getElementById('headroom-bar').style.width = '0%';
        document.getElementById('headroom-stats').innerHTML =
            '<span><span class="label">proxy not active</span></span>';
    }

    // Sparklines
    if (d.sparklines) {
        for (var i = 0; i < TOOLS.length; i++) {
            var t = TOOLS[i];
            var sp = d.sparklines[t];
            var pts = sp ? (sp.points || sp) : [];
            renderSparkline(document.getElementById(t + '-sparkline'), pts, t);

            var deltaEl = document.getElementById(t + '-delta');
            var delta = sp ? (sp.delta || 0) : 0;
            if (delta > 0) {
                deltaEl.textContent = '+' + formatTokens(delta);
                deltaEl.className = 'card-delta active';
            } else {
                deltaEl.textContent = '';
                deltaEl.className = 'card-delta';
            }
        }
    }

    // Feed
    var feedEl = document.getElementById('feed');
    var hist = d.history || [];
    var lines = [];
    for (var j = 0; j < hist.length; j++) {
        var h = hist[j];
        var toolClr = TOOL_COLOURS[h.tool] || '#888';
        var savingsClass = 'info';
        var savingsText = '';
        if (h.saved_tokens > 0) {
            savingsClass = 'positive';
            savingsText = '-' + formatTokens(h.saved_tokens) + ' tokens (' + (h.saved_pct || 0) + '%)';
        } else if (h.saved_tokens === 0 && h.saved_pct === 0) {
            savingsClass = 'zero';
            savingsText = 'no savings';
        } else {
            savingsText = formatTokens(h.saved_tokens || 0) + ' tokens';
        }
        var lineClasses = 'feed-line';
        if (h.saved_tokens === 0 && (h.saved_pct === 0 || !h.saved_pct)) {
            lineClasses += ' muted';
        } else if (h.saved_tokens > 0) {
            lineClasses += ' highlight';
        }
        if (h.grouped) {
            lineClasses += ' grouped';
        }
        lines.push(
            '<div class="' + lineClasses + '"' + (lineClasses.indexOf('highlight') >= 0 || lineClasses.indexOf('grouped') >= 0 ? ' style="border-color:' + toolClr + '"' : '') + '>' +
            '<span class="feed-time">' + shortTime(h.time) + '</span>' +
            '<span class="feed-tool" style="color:' + toolClr + '">' + (h.tool || '') + '</span>' +
            '<span class="feed-cmd">' + escHtml(h.cmd || '') + '</span>' +
            '<span class="feed-savings ' + savingsClass + '">' + savingsText + '</span>' +
            '</div>'
        );
    }
    feedEl.innerHTML = lines.join('');

    var countEl = document.querySelector('.feed-count');
    if (countEl) {
        countEl.textContent = 'showing last ' + hist.length;
    }
}

function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Clock
function updateClock() {
    var now = new Date();
    var h = String(now.getHours()).padStart(2, '0');
    var m = String(now.getMinutes()).padStart(2, '0');
    var s = String(now.getSeconds()).padStart(2, '0');
    var months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    var day = String(now.getDate()).padStart(2, '0');
    var mon = months[now.getMonth()];
    var yr = now.getFullYear();
    document.getElementById('clock').textContent = h + ':' + m + ':' + s + ' \u25aa ' + day + ' ' + mon + ' ' + yr;
}
setInterval(updateClock, 1000);
updateClock();
wireUsageToggle();

// SSE
var source = new EventSource('/events');
source.onmessage = function(e) {
    try {
        var d = JSON.parse(e.data);
        updateDashboard(d);
    } catch (err) {
        console.error('SSE parse error:', err);
    }
};
source.onerror = function() {
    console.debug('SSE connection lost, will retry...');
};
</script>
</body>
</html>"""


# --- Routes ---

@app.route("/")
def index():
    return HTML


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def api_status():
    flat = _flatten_snapshot(_collector.snapshot())
    return Response(
        json.dumps(flat),
        mimetype="application/json",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/events")
def events():
    def stream():
        while True:
            payload = _collector.snapshot() or {}
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(SSE_INTERVAL)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
