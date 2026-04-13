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
JCODEMUNCH_INDEX_DIR = os.environ.get("JCODEMUNCH_INDEX_DIR", os.path.join(HOME, ".code-index"))
JCODEMUNCH_BIN = os.environ.get("JCODEMUNCH_BIN", "jcodemunch-mcp")
JDOCMUNCH_INDEX_DIR = os.environ.get("JDOCMUNCH_INDEX_DIR", os.path.join(HOME, ".doc-index"))
JDOCMUNCH_BIN = os.environ.get("JDOCMUNCH_BIN", "jdocmunch-mcp")
PORT = int(os.environ.get("PORT", "8095"))
SSE_INTERVAL = int(os.environ.get("SSE_INTERVAL", "2"))
COLLECTOR_INTERVAL = float(os.environ.get("COLLECTOR_INTERVAL", "0.25"))
CLAUDE_CREDENTIALS = os.environ.get("CLAUDE_CREDENTIALS", os.path.join(HOME, ".claude", ".credentials.json"))
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_POLL_INTERVAL = 180  # 3 minutes, same as ccstatusline
USAGE_BACKOFF = 300  # 5 minutes on 429
WEEKLY_CACHE_DIR = os.environ.get("WEEKLY_CACHE_DIR", os.path.join(HOME, ".cache", "claude-tools-dashboard"))

# Persistent state for sparklines and fallback
_last_good = {}
_usage_cache = None
_usage_cache_time = 0
_sparkline_buffers = {
    "rtk": deque(maxlen=240),
    "headroom": deque(maxlen=240),
    "jcodemunch": deque(maxlen=240),
    "jdocmunch": deque(maxlen=240),
}
_headroom_last_total = 0
_headroom_history = []
_jcodemunch_last_total = 0
_jcodemunch_last_mtime = 0
_jcodemunch_history = []
_jdocmunch_last_total = 0
_jdocmunch_last_mtime = 0
_jdocmunch_history = []
_last_collect_success = {
    "rtk": 0.0,
    "headroom": 0.0,
    "jcodemunch": 0.0,
    "jdocmunch": 0.0,
}

# Cached version strings. resolve_versions_once() populates this at module
# import, and the collectors read from it on every tick to avoid subprocess
# on the hot path.
_cached_versions = {
    "rtk": None,
    "jcodemunch": None,
    "jdocmunch": None,
}


def _read_jcodemunch_config_version():
    config_path = os.path.join(JCODEMUNCH_INDEX_DIR, "config.jsonc")
    try:
        with open(config_path) as f:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
            return m.group(1) if m else None
    except OSError:
        return None


def resolve_versions_once():
    """Resolve tool versions once. Called by DashboardCollector.run() at thread startup.
    Per-tool precedence: TOOL_VERSION env var > binary --version > tool-specific fallback > "unknown"."""
    rtk_v = os.environ.get("RTK_VERSION") or _run([RTK_BIN, "--version"])
    _cached_versions["rtk"] = rtk_v if rtk_v else "unknown"

    jc_v = (
        os.environ.get("JCODEMUNCH_VERSION")
        or _run([JCODEMUNCH_BIN, "--version"])
        or _read_jcodemunch_config_version()
    )
    _cached_versions["jcodemunch"] = jc_v if jc_v else "unknown"

    jd_v = os.environ.get("JDOCMUNCH_VERSION") or _run([JDOCMUNCH_BIN, "--version"])
    if not jd_v:
        raw = _run(["pipx", "list", "--short"])
        if raw:
            for line in raw.splitlines():
                if "jdocmunch" in line:
                    parts = line.strip().split()
                    jd_v = parts[1] if len(parts) > 1 else None
                    break
    _cached_versions["jdocmunch"] = jd_v if jd_v else "unknown"


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


def collect_headroom():
    """Check headroom proxy stats endpoint."""
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
            resp = urlopen(f"{HEADROOM_URL}/stats", timeout=2)
            raw = json.loads(resp.read().decode())

            tokens_section = raw.get("tokens") or {}
            cache_section = raw.get("compression_cache") or {}
            display = raw.get("display_session") or {}
            persist = (raw.get("persistent_savings") or {}).get("lifetime") or {}
            req_stats = raw.get("requests") or {}
            latency = raw.get("latency") or {}
            prefix_totals = ((raw.get("prefix_cache") or {}).get("totals") or {})

            total_saved = tokens_section.get("saved", 0)
            avg_pct = round(tokens_section.get("savings_percent", 0), 1)

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
                "cache_hit_rate": round(prefix_totals.get("hit_rate", 0), 1),
                "requests_total": req_stats.get("total", 0),
                "requests_failed": req_stats.get("failed", 0),
                "avg_latency_ms": round(latency.get("average_ms", 0), 1),
                "history": list(_headroom_history),
            }
        except (URLError, OSError, json.JSONDecodeError):
            return {
                "active": False,
                "version": version,
            }
    except Exception:
        return None


def collect_jcodemunch():
    """Read jcodemunch savings and index stats."""
    try:
        index_dir = JCODEMUNCH_INDEX_DIR
        if not os.path.isdir(index_dir):
            return None

        savings_path = os.path.join(index_dir, "_savings.json")
        total_tokens_saved = 0
        if os.path.exists(savings_path):
            with open(savings_path) as f:
                data = json.load(f)
            total_tokens_saved = data.get("total_tokens_saved", 0)

        # Count .db files and sum sizes
        index_dir = JCODEMUNCH_INDEX_DIR
        db_files = glob.glob(os.path.join(index_dir, "*.db"))
        repos_indexed = len(db_files)
        index_size_bytes = sum(os.path.getsize(f) for f in db_files if os.path.exists(f))
        index_size_mb = round(index_size_bytes / (1024 * 1024), 1)

        version = _cached_versions.get("jcodemunch") or "unknown"

        # Detect activity via the newest mtime across session_stats.json, _savings.json,
        # and all per-repo .db files. jcodemunch-mcp flushes these at different times
        # (neither file updates on every MCP call), so watching the max catches more events.
        global _jcodemunch_last_total, _jcodemunch_last_mtime, _jcodemunch_history
        stats_path = os.path.join(index_dir, "session_stats.json")
        watched_paths = [stats_path, savings_path, *db_files]
        newest_mtime = max(
            (os.path.getmtime(p) for p in watched_paths if os.path.exists(p)),
            default=0,
        )
        if newest_mtime > _jcodemunch_last_mtime and _jcodemunch_last_mtime > 0:
            if total_tokens_saved > _jcodemunch_last_total and _jcodemunch_last_total > 0:
                delta = total_tokens_saved - _jcodemunch_last_total
                _jcodemunch_history.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "tool": "jcodemunch",
                    "cmd": f"indexed/queried across {repos_indexed} repos",
                    "saved_tokens": delta,
                    "saved_pct": 0,
                })
            else:
                _jcodemunch_history.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "tool": "jcodemunch",
                    "cmd": f"query across {repos_indexed} repos ({index_size_mb}MB indexed)",
                    "saved_tokens": 0,
                    "saved_pct": 0,
                })
            _jcodemunch_history = _jcodemunch_history[-100:]
        _jcodemunch_last_mtime = newest_mtime
        _jcodemunch_last_total = total_tokens_saved

        # Freshness: 100% if active in last 5 min, decays to 0% over 60 min
        if newest_mtime > 0:
            elapsed_min = (time.time() - newest_mtime) / 60
            freshness = max(0, round(100 - (elapsed_min / 60 * 100)))
        else:
            freshness = 0

        if freshness > 0:
            if elapsed_min < 1:
                freshness_label = "just now"
            elif elapsed_min < 60:
                freshness_label = f"{int(elapsed_min)}m ago"
            else:
                freshness_label = f"{int(elapsed_min / 60)}h ago"
        else:
            freshness_label = "idle"

        return {
            "active": repos_indexed > 0 or total_tokens_saved > 0,
            "total_saved": total_tokens_saved,
            "repos_indexed": repos_indexed,
            "index_size_mb": index_size_mb,
            "version": version or "unknown",
            "history": list(_jcodemunch_history),
            "freshness": freshness,
            "freshness_label": freshness_label,
        }
    except Exception:
        return None


def collect_jdocmunch():
    global _jdocmunch_last_total, _jdocmunch_last_mtime, _jdocmunch_history
    try:
        index_dir = JDOCMUNCH_INDEX_DIR
        if not os.path.isdir(index_dir):
            return None

        savings_path = os.path.join(index_dir, "_savings.json")
        total_tokens_saved = 0
        if os.path.exists(savings_path):
            with open(savings_path) as f:
                data = json.load(f)
            total_tokens_saved = data.get("total_tokens_saved", 0)

        index_files = [f for f in glob.glob(os.path.join(index_dir, "local", "*.json"))]
        docs_indexed = len(index_files)
        index_size_mb = round(sum(os.path.getsize(f) for f in index_files) / (1024 * 1024), 1)

        # Version (cached at startup)
        version = _cached_versions.get("jdocmunch") or "unknown"

        # _savings.json updates on every get_section call (token savings).
        # Index JSON files update on re-index. Check both for activity.
        savings_mtime = os.path.getmtime(savings_path) if os.path.exists(savings_path) else 0
        idx_mtime = max(
            (os.path.getmtime(f) for f in index_files),
            default=0,
        )
        newest_mtime = max(savings_mtime, idx_mtime)

        if newest_mtime > _jdocmunch_last_mtime and _jdocmunch_last_mtime > 0:
            delta = total_tokens_saved - _jdocmunch_last_total
            if delta > 0:
                _jdocmunch_history.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "tool": "jdocmunch",
                    "cmd": f"indexed/queried across {docs_indexed} docs",
                    "saved_tokens": delta,
                    "saved_pct": 0,
                })
            else:
                _jdocmunch_history.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "tool": "jdocmunch",
                    "cmd": f"query across {docs_indexed} docs ({index_size_mb}MB indexed)",
                    "saved_tokens": 0,
                    "saved_pct": 0,
                })
            _jdocmunch_history = _jdocmunch_history[-100:]
        _jdocmunch_last_mtime = newest_mtime
        _jdocmunch_last_total = total_tokens_saved

        # Freshness: 100% if active in last 5 min, decays to 0% over 60 min
        if newest_mtime > 0:
            elapsed_min = (time.time() - newest_mtime) / 60
            freshness = max(0, round(100 - (elapsed_min / 60 * 100)))
        else:
            freshness = 0

        if freshness > 0:
            if elapsed_min < 1:
                freshness_label = "just now"
            elif elapsed_min < 60:
                freshness_label = f"{int(elapsed_min)}m ago"
            else:
                freshness_label = f"{int(elapsed_min / 60)}h ago"
        else:
            freshness_label = "idle"

        return {
            "active": docs_indexed > 0 or total_tokens_saved > 0,
            "total_saved": total_tokens_saved,
            "docs_indexed": docs_indexed,
            "index_size_mb": index_size_mb,
            "version": version,
            "history": list(_jdocmunch_history),
            "freshness": freshness,
            "freshness_label": freshness_label,
        }
    except Exception:
        return None


def _read_oauth_token():
    """Read Claude Code OAuth access token from credentials file."""
    try:
        with open(CLAUDE_CREDENTIALS) as f:
            creds = json.load(f)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def collect_claude_usage():
    """Fetch Claude usage from Anthropic API with 3-minute cache."""
    global _usage_cache, _usage_cache_time

    now = time.time()
    if _usage_cache and (now - _usage_cache_time) < USAGE_POLL_INTERVAL:
        return _usage_cache

    token = _read_oauth_token()
    if not token:
        return _usage_cache  # return stale cache or None

    try:
        req = Request(USAGE_API_URL)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", "oauth-2025-04-20")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        five = data.get("five_hour") or {}
        seven = data.get("seven_day") or {}
        sonnet = data.get("seven_day_sonnet") or {}
        extra = data.get("extra_usage") or {}
        extra_enabled = bool(extra.get("is_enabled"))

        result = {
            "session_pct": five.get("utilization"),
            "session_reset": five.get("resets_at"),
            "weekly_pct": seven.get("utilization"),
            "weekly_reset": seven.get("resets_at"),
            "sonnet_pct": sonnet.get("utilization"),
            "sonnet_reset": sonnet.get("resets_at"),
            "extra_usage_enabled": extra_enabled,
            "extra_usage_monthly_limit": extra.get("monthly_limit") if extra_enabled else None,
            "extra_usage_used": extra.get("used_credits") if extra_enabled else None,
            "extra_usage_pct": extra.get("utilization") if extra_enabled else None,
            "active": True,
        }
        _usage_cache = result
        _usage_cache_time = now
        return result
    except HTTPError as e:
        if e.code == 429:
            # Back off on rate limit -- extend cache validity
            _usage_cache_time = now - USAGE_POLL_INTERVAL + USAGE_BACKOFF
        return _usage_cache
    except Exception:
        return _usage_cache


def _load_weekly_cache():
    """Load weekly savings snapshot from disk."""
    path = os.path.join(WEEKLY_CACHE_DIR, "weekly.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_weekly_cache(data):
    """Save weekly savings snapshot to disk."""
    os.makedirs(WEEKLY_CACHE_DIR, exist_ok=True)
    path = os.path.join(WEEKLY_CACHE_DIR, "weekly.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


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


def _same_reset_window(a, b):
    """Return True iff two ISO-8601 resets_at timestamps refer to the same reset window.

    Anthropic's usage API returns resets_at with sub-second precision that
    jitters (observed drift up to ~1s) between calls even within the same
    week. Compare truncated to the minute so drift does not trigger false
    week rotations in collect_all.
    """
    if not a or not b:
        return False
    try:
        da = datetime.fromisoformat(a).replace(second=0, microsecond=0)
        db = datetime.fromisoformat(b).replace(second=0, microsecond=0)
        return da == db
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
    jcm = snap.get("jcodemunch") or {}
    jdm = snap.get("jdocmunch") or {}

    spark_rtk = sparklines.get("rtk") or {}
    spark_hr = sparklines.get("headroom") or {}
    spark_jcm = sparklines.get("jcodemunch") or {}
    spark_jdm = sparklines.get("jdocmunch") or {}

    claude_active = bool(claude.get("active"))
    def _claude(key):
        return claude.get(key) if claude_active else None

    hr_lifetime = headroom.get("lifetime_saved") or 0
    hr_lifetime_usd = headroom.get("lifetime_saved_usd") or 0
    usd_per_token = (hr_lifetime_usd / hr_lifetime) if hr_lifetime > 0 else None
    combined_saved_usd = None
    if usd_per_token is not None:
        non_headroom_tokens = (
            (rtk.get("total_saved") or 0)
            + (jcm.get("total_saved") or 0)
            + (jdm.get("total_saved") or 0)
        )
        combined_saved_usd = hr_lifetime_usd + non_headroom_tokens * usd_per_token

    return {
        "ready": ready,
        "timestamp": snap.get("timestamp"),

        "claude_active": claude_active,
        "session_pct": _claude("session_pct"),
        "session_reset": _claude("session_reset"),
        "weekly_pct": _claude("weekly_pct"),
        "weekly_reset": _claude("weekly_reset"),
        "weekly_reset_display": weekly.get("reset_display") or None,
        "sonnet_pct": _claude("sonnet_pct"),
        "sonnet_reset": _claude("sonnet_reset"),

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

        "jcodemunch_active": jcm.get("active", False),
        "jcodemunch_health": jcm.get("health", "error"),
        "jcodemunch_version": jcm.get("version", "unknown"),
        "jcodemunch_saved": jcm.get("total_saved", 0),
        "jcodemunch_delta": spark_jcm.get("delta", 0),
        "jcodemunch_repos_indexed": jcm.get("repos_indexed", 0),
        "jcodemunch_index_size_mb": jcm.get("index_size_mb", 0),
        "jcodemunch_freshness": jcm.get("freshness", 0),
        "jcodemunch_freshness_label": jcm.get("freshness_label", "idle"),

        "jdocmunch_active": jdm.get("active", False),
        "jdocmunch_health": jdm.get("health", "error"),
        "jdocmunch_version": jdm.get("version", "unknown"),
        "jdocmunch_saved": jdm.get("total_saved", 0),
        "jdocmunch_delta": spark_jdm.get("delta", 0),
        "jdocmunch_docs_indexed": jdm.get("docs_indexed", 0),
        "jdocmunch_index_size_mb": jdm.get("index_size_mb", 0),
        "jdocmunch_freshness": jdm.get("freshness", 0),
        "jdocmunch_freshness_label": jdm.get("freshness_label", "idle"),

        "extra_usage_enabled": bool(_claude("extra_usage_enabled")),
        "extra_usage_monthly_limit": _claude("extra_usage_monthly_limit"),
        "extra_usage_used": _claude("extra_usage_used"),
        "extra_usage_pct": _claude("extra_usage_pct"),
    }


def collect_all():
    """Collect from all tools, maintain sparklines and fallbacks."""
    global _last_good

    collectors = {
        "rtk": collect_rtk,
        "headroom": collect_headroom,
        "jcodemunch": collect_jcodemunch,
        "jdocmunch": collect_jdocmunch,
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

    # Weekly savings tracking
    claude_usage = collect_claude_usage()
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

    # Format reset display: "Thu 3 Apr 15:00"
    reset_display = ""
    if claude_usage and claude_usage.get("weekly_reset"):
        try:
            reset_dt = datetime.fromisoformat(claude_usage["weekly_reset"])
            reset_display = reset_dt.strftime("%a %-d %b %H:%M")
        except (ValueError, TypeError):
            reset_display = ""

    # Update sparkline buffers with cumulative totals
    now = time.time()
    for name in collectors:
        tool_data = results[name]
        if not tool_data.get("active"):
            continue
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
    if "history" in results.get("jcodemunch", {}):
        history.extend(results["jcodemunch"]["history"])
    if "history" in results.get("jdocmunch", {}):
        history.extend(results["jdocmunch"]["history"])

    # Sort by time descending, collapse bursts, limit to 50
    history.sort(key=lambda x: x.get("time", ""), reverse=True)
    history = _group_history(history)
    history = history[:50]

    # Per-tool history lists are merged into the top-level `history` above,
    # so drop them to shave ~15KB per SSE tick.
    for tool_name in ("rtk", "headroom", "jcodemunch", "jdocmunch"):
        results[tool_name].pop("history", None)

    timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "timestamp": timestamp,
        "combined_saved": combined_saved,
        "rtk": results["rtk"],
        "headroom": results["headroom"],
        "jcodemunch": results["jcodemunch"],
        "jdocmunch": results["jdocmunch"],
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

/* Tool accent colours — purely for card identity, no semantic meaning */
.clr-rtk { color: #3b82f6; }
.fill-rtk { background: #3b82f6; }
.stroke-rtk { stroke: #3b82f6; }
.area-rtk { fill: rgba(59, 130, 246, 0.1); }

.clr-headroom { color: #8b5cf6; }
.fill-headroom { background: #8b5cf6; }
.stroke-headroom { stroke: #8b5cf6; }
.area-headroom { fill: rgba(139, 92, 246, 0.1); }

.clr-jcodemunch { color: #ec4899; }
.fill-jcodemunch { background: #ec4899; }
.stroke-jcodemunch { stroke: #ec4899; }
.area-jcodemunch { fill: rgba(236, 72, 153, 0.1); }

.clr-jdocmunch { color: #14b8a6; }
.fill-jdocmunch { background: #14b8a6; }
.stroke-jdocmunch { stroke: #14b8a6; }
.area-jdocmunch { fill: rgba(20, 184, 166, 0.1); }
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
/* Summary cards */
.summary-cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 12px;
}
.summary-cards .card.card-combined {
    grid-column: span 2;
}
.summary-cards .card-value {
    color: #fff;
    margin-bottom: 0;
}
.summary-cards .card-value.dim {
    color: #666;
}
.summary-cards .card-combined .card-value {
    color: #00ff88;
}
.summary-cards .card-claude .card-version {
    color: #bbb;
}
.summary-cards .card-sub {
    color: #aaa;
    font-size: 13px;
    margin-top: 4px;
    margin-bottom: 0;
}
.summary-cards .card-extra .progress-track {
    margin-top: 12px;
}
.summary-cards .combined-body {
    display: flex;
    align-items: center;
    gap: 48px;
}
.summary-cards .combined-left {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
}
.summary-cards .val-time {
    color: #bbb;
    font-weight: 600;
}
.summary-cards .card-sub-usd {
    color: #5cc48a;
    font-size: 12px;
    margin-top: 2px;
}
.summary-cards .combined-stats {
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex: 1 1 0;
    min-width: 0;
}
.summary-cards .combined-stats .stat-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
}
.summary-cards .combined-stats .label {
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-size: 10px;
}
.summary-cards .combined-stats .val {
    color: #fff;
    font-weight: 600;
    font-size: 14px;
}
.summary-cards .val-live { color: #00ff88; }
.summary-cards .val-rate { color: #5cc48a; }
.summary-cards .val-cold { color: #666; }
.summary-cards .usage-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 4px 0;
    font-size: 13px;
    color: #aaa;
}
.summary-cards .usage-row .label { color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
.summary-cards .usage-row .val { color: #fff; font-weight: 600; }
.summary-cards .usage-row .val.pct-green { color: #00ff88; }
.summary-cards .usage-row .val.pct-yellow { color: #ffcc00; }
.summary-cards .usage-row .val.pct-red { color: #ff4444; }
.summary-cards .card-extra .progress-fill.pct-green { background: #00ff88; }
.summary-cards .card-extra .progress-fill.pct-yellow { background: #ffcc00; }
.summary-cards .card-extra .progress-fill.pct-red { background: #ff4444; }
@media (max-width: 1000px) {
    .summary-cards { grid-template-columns: repeat(2, 1fr); }
    .summary-cards .card.card-combined { grid-column: span 2; }
}
@media (max-width: 600px) {
    .header { flex-wrap: wrap; justify-content: center; gap: 4px; }
    .header-left { width: 100%; justify-content: center; }
    .header-right { font-size: 12px; }
    html, body { overflow: auto; height: auto; }
    .summary-cards { grid-template-columns: 1fr; }
    .summary-cards .card.card-combined { grid-column: auto; }
    .summary-cards .card-header {
        flex-wrap: wrap;
        gap: 4px 8px;
    }
    .summary-cards .combined-body {
        flex-direction: column;
        align-items: stretch;
        gap: 16px;
    }
    .summary-cards .combined-body .card-value {
        flex: none;
    }
    .summary-cards .combined-stats {
        flex: none;
    }
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

<!-- Summary Cards -->
<div class="summary-cards">
    <div class="card card-combined" id="summary-combined">
        <div class="card-header">
            <span class="health-dot health-ok" id="summary-combined-health"></span>
            <span class="card-name">Combined</span>
        </div>
        <div class="combined-body">
            <div class="combined-left">
                <div class="card-value" id="summary-combined-value">--</div>
                <div class="card-sub">tokens saved</div>
                <div class="card-sub card-sub-usd" id="summary-combined-usd">--</div>
            </div>
            <div class="combined-stats">
                <div class="stat-row"><span class="label">This Week</span><span class="val val-live" id="summary-this-week">--</span></div>
                <div class="stat-row"><span class="label">Last Week</span><span class="val val-cold" id="summary-last-week">--</span></div>
                <div class="stat-row"><span class="label">Avg/Day</span><span class="val val-rate" id="summary-burn">--</span></div>
            </div>
        </div>
    </div>
    <div class="card card-claude" id="summary-claude">
        <div class="card-header">
            <span class="health-dot health-error" id="summary-claude-health"></span>
            <span class="card-name">Claude Usage</span>
        </div>
        <div class="usage-row"><span class="label">5-Hour</span><span class="val" id="summary-session-pct">--</span></div>
        <div class="usage-row"><span class="label">Weekly</span><span class="val" id="summary-weekly-pct">--</span></div>
        <div class="usage-row"><span class="label">Sonnet</span><span class="val" id="summary-sonnet-pct">--</span></div>
        <div class="usage-row"><span class="label">Reset</span><span class="val val-time" id="summary-claude-reset">--</span></div>
    </div>
    <div class="card card-extra inactive" id="summary-extra">
        <div class="card-header">
            <span class="health-dot health-error" id="summary-extra-health"></span>
            <span class="card-name">Extra Usage</span>
            <span class="card-version">overage</span>
        </div>
        <div class="card-value dim" id="summary-extra-value">n/a</div>
        <div class="card-sub" id="summary-extra-detail">not enabled</div>
        <div class="progress-track"><div class="progress-fill" id="summary-extra-bar" style="width:0%"></div></div>
    </div>
</div>

<!-- Cards -->
<div class="cards">
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

    <!-- jCodeMunch -->
    <div class="card" id="jcodemunch-card">
        <div class="card-header">
            <span class="health-dot health-error" id="jcodemunch-health"></span><a href="https://github.com/jgravelle/jcodemunch-mcp" target="_blank" class="card-name">jCodeMunch</a>
            <span class="card-version" id="jcodemunch-version">--</span>
        </div>
        <div class="card-value clr-jcodemunch" id="jcodemunch-value">--</div>
        <div class="card-sub" id="jcodemunch-sub">tokens saved</div>
        <div class="progress-track"><div class="progress-fill fill-jcodemunch" id="jcodemunch-bar" style="width:0%"></div></div>
        <div class="card-stats" id="jcodemunch-stats">
            <span><span class="label">repos</span> <span class="val">--</span></span>
        </div>
        <div class="sparkline-container"><svg id="jcodemunch-sparkline" viewBox="0 0 200 35" preserveAspectRatio="none"></svg></div>
        <div class="card-delta" id="jcodemunch-delta"></div>
    </div>

    <!-- jDocMunch -->
    <div class="card" id="jdocmunch-card">
        <div class="card-header">
            <span class="health-dot health-error" id="jdocmunch-health"></span><a href="https://github.com/jgravelle/jdocmunch-mcp" target="_blank" class="card-name">jDocMunch</a>
            <span class="card-version" id="jdocmunch-version">--</span>
        </div>
        <div class="card-value clr-jdocmunch" id="jdocmunch-value">--</div>
        <div class="card-sub" id="jdocmunch-sub">tokens saved</div>
        <div class="progress-track"><div class="progress-fill fill-jdocmunch" id="jdocmunch-bar" style="width:0%"></div></div>
        <div class="card-stats" id="jdocmunch-stats">
            <span><span class="label">docs</span> <span class="val">--</span></span>
        </div>
        <div class="sparkline-container"><svg id="jdocmunch-sparkline" viewBox="0 0 200 35" preserveAspectRatio="none"></svg></div>
        <div class="card-delta" id="jdocmunch-delta"></div>
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
    // ISO: 2026-03-28T22:40:32.029588405+00:00 -> 22:40:32
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

var TOOLS = ['rtk', 'headroom', 'jcodemunch', 'jdocmunch'];
var TOOL_COLOURS = {
    rtk: '#3b82f6',
    headroom: '#8b5cf6',
    jcodemunch: '#ec4899',
    jdocmunch: '#14b8a6'
};

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
    // Summary cards
    var w = d.weekly || {};
    var cu = d.claude_usage || {};

    // --- Combined card ---
    document.getElementById('summary-combined-health').className = 'health-dot ' + (d.ready === false ? 'health-error' : 'health-ok');
    document.getElementById('summary-combined-value').textContent = formatTokens(d.combined_saved || 0);
    var combinedUsdEl = document.getElementById('summary-combined-usd');
    var hrLifetime = (d.headroom || {}).lifetime_saved || 0;
    var hrLifetimeUsd = (d.headroom || {}).lifetime_saved_usd || 0;
    var rate = hrLifetime > 0 ? hrLifetimeUsd / hrLifetime : null;
    if (rate != null) {
        var nonHrTokens = ((d.rtk || {}).total_saved || 0) + ((d.jcodemunch || {}).total_saved || 0) + ((d.jdocmunch || {}).total_saved || 0);
        var combinedUsd = hrLifetimeUsd + nonHrTokens * rate;
        combinedUsdEl.textContent = '≈ $' + combinedUsd.toFixed(2) + ' saved';
    } else {
        combinedUsdEl.textContent = '--';
    }
    document.getElementById('summary-this-week').textContent = w.week_is_fresh ? '--' : (w.this_week != null ? formatTokens(w.this_week, true) : '--');
    document.getElementById('summary-last-week').textContent = w.last_week != null ? (w.last_week === 0 ? '0' : formatTokens(w.last_week, true)) : '--';
    document.getElementById('summary-burn').textContent = w.burn_rate_daily != null ? (w.burn_rate_daily === 0 ? '0' : formatTokens(w.burn_rate_daily, true)) : '--';

    // --- Claude Usage card ---
    document.getElementById('summary-claude-health').className = 'health-dot ' + (cu.active ? 'health-ok' : 'health-error');
    applyPctField(document.getElementById('summary-session-pct'), cu.active ? cu.session_pct : null);
    applyPctField(document.getElementById('summary-weekly-pct'), cu.active ? cu.weekly_pct : null);
    applyPctField(document.getElementById('summary-sonnet-pct'), cu.active ? cu.sonnet_pct : null);
    document.getElementById('summary-claude-reset').textContent = w.reset_display || '--';

    // --- Extra Usage card ---
    var extraCard = document.getElementById('summary-extra');
    var extraVal = document.getElementById('summary-extra-value');
    var extraDetail = document.getElementById('summary-extra-detail');
    var extraBar = document.getElementById('summary-extra-bar');
    document.getElementById('summary-extra-health').className = 'health-dot ' + (cu.active && cu.extra_usage_enabled ? 'health-ok' : 'health-error');
    if (cu.active && cu.extra_usage_enabled) {
        extraCard.className = 'card card-extra';
        var extraPct = cu.extra_usage_pct || 0;
        extraVal.className = 'card-value ' + pctClass(extraPct);
        extraVal.textContent = (cu.extra_usage_pct != null ? cu.extra_usage_pct.toFixed(1) : '0') + '%';
        var used = cu.extra_usage_used;
        var limit = cu.extra_usage_monthly_limit;
        if (used != null && limit != null) {
            extraDetail.textContent = used.toLocaleString() + ' of ' + limit.toLocaleString() + ' credits';
        } else {
            extraDetail.textContent = 'active';
        }
        extraBar.style.width = Math.min(100, Math.max(0, extraPct)) + '%';
        extraBar.className = 'progress-fill ' + pctClass(extraPct);
    } else {
        extraCard.className = 'card card-extra inactive';
        extraVal.className = 'card-value dim';
        extraVal.textContent = 'n/a';
        extraDetail.textContent = 'not enabled';
        extraBar.style.width = '0%';
        extraBar.className = 'progress-fill';
    }

    // RTK
    var rtk = d.rtk || {};
    var rtkCard = document.getElementById('rtk-card');
    rtkCard.className = rtk.active ? 'card' : 'card inactive';
    var rtkHealth = rtk.health || 'error';
    var rtkDot = document.getElementById('rtk-health');
    if (rtkDot) rtkDot.className = 'health-dot health-' + rtkHealth;
    document.getElementById('rtk-version').textContent = shortVersion(rtk.version);
    document.getElementById('rtk-value').textContent = rtk.active ? formatTokens(rtk.total_saved || 0) : '--';
    document.getElementById('rtk-sub').textContent = 'tokens saved';
    document.getElementById('rtk-bar').style.width = (rtk.avg_savings_pct || 0) + '%';
    if (rtk.active && rtk.total_commands) {
        var avgMs = rtk.total_time_ms / rtk.total_commands;
        document.getElementById('rtk-stats').innerHTML =
            '<span><span class="label">efficiency</span> <span class="val">' + (rtk.avg_savings_pct || 0) + '%</span></span>' +
            '<span><span class="label">cmds</span> <span class="val">' + rtk.total_commands + '</span></span>' +
            '<span><span class="label">avg</span> <span class="val">' + formatTime(avgMs) + '</span></span>';
    }

    // Headroom
    var hr = d.headroom || {};
    var hrCard = document.getElementById('headroom-card');
    hrCard.className = hr.active ? 'card' : 'card inactive';
    var hrHealth = hr.health || 'error';
    var hrDot = document.getElementById('headroom-health');
    if (hrDot) hrDot.className = 'health-dot health-' + hrHealth;
    document.getElementById('headroom-version').textContent = shortVersion(hr.version);
    if (hr.active) {
        var hrLifetime = hr.lifetime_saved || hr.total_saved || 0;
        var hrSessionUsd = hr.session_saved_usd || 0;
        document.getElementById('headroom-value').textContent = formatTokens(hrLifetime);
        document.getElementById('headroom-sub').textContent = 'tokens saved';
        document.getElementById('headroom-bar').style.width = (hr.avg_savings_pct || 0) + '%';
        document.getElementById('headroom-stats').innerHTML =
            '<span><span class="label">session</span> <span class="val">$' + hrSessionUsd.toFixed(2) + '</span></span>' +
            '<span><span class="label">cache</span> <span class="val">' + Math.round(hr.cache_hit_rate || 0) + '%</span></span>' +
            '<span><span class="label">req</span> <span class="val">' + (hr.requests_total || 0) + '</span></span>';
    } else {
        document.getElementById('headroom-value').textContent = '--';
        document.getElementById('headroom-sub').textContent = 'awaiting first session';
        document.getElementById('headroom-bar').style.width = '0%';
        document.getElementById('headroom-stats').innerHTML =
            '<span><span class="label">proxy not active</span></span>';
    }

    // jCodeMunch
    var jc = d.jcodemunch || {};
    var jcCard = document.getElementById('jcodemunch-card');
    jcCard.className = jc.active ? 'card' : 'card inactive';
    var jcHealth = jc.health || 'error';
    var jcDot = document.getElementById('jcodemunch-health');
    if (jcDot) jcDot.className = 'health-dot health-' + jcHealth;
    document.getElementById('jcodemunch-version').textContent = shortVersion(jc.version);
    document.getElementById('jcodemunch-value').textContent = jc.active ? formatTokens(jc.total_saved || 0) : '--';
    document.getElementById('jcodemunch-sub').textContent = 'tokens saved';
    document.getElementById('jcodemunch-bar').style.width = (jc.freshness || 0) + '%';
    if (jc.active) {
        document.getElementById('jcodemunch-stats').innerHTML =
            '<span><span class="label">repos</span> <span class="val">' + (jc.repos_indexed || 0) + '</span></span>' +
            '<span><span class="label">indexed</span> <span class="val">' + (jc.index_size_mb || 0) + 'MB</span></span>' +
            '<span><span class="label">active</span> <span class="val">' + (jc.freshness_label || 'idle') + '</span></span>';
    }

    // jDocMunch
    var jd = d.jdocmunch || {};
    var jdCard = document.getElementById('jdocmunch-card');
    jdCard.className = jd.active ? 'card' : 'card inactive';
    var jdHealth = jd.health || 'error';
    var jdDot = document.getElementById('jdocmunch-health');
    if (jdDot) jdDot.className = 'health-dot health-' + jdHealth;
    document.getElementById('jdocmunch-version').textContent = shortVersion(jd.version);
    document.getElementById('jdocmunch-value').textContent = jd.active ? formatTokens(jd.total_saved || 0) : '--';
    document.getElementById('jdocmunch-sub').textContent = 'tokens saved';
    document.getElementById('jdocmunch-bar').style.width = (jd.freshness || 0) + '%';
    if (jd.active) {
        document.getElementById('jdocmunch-stats').innerHTML =
            '<span><span class="label">docs</span> <span class="val">' + (jd.docs_indexed || 0) + '</span></span>' +
            '<span><span class="label">indexed</span> <span class="val">' + (jd.index_size_mb || 0) + 'MB</span></span>' +
            '<span><span class="label">active</span> <span class="val">' + (jd.freshness_label || 'idle') + '</span></span>';
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
