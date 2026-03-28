"""Claude Tools Dashboard -- Flask backend with SSE streaming."""

import json
import os
import glob
import sqlite3
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import URLError

from flask import Flask, Response, jsonify

app = Flask(__name__)

HOME = os.path.expanduser("~")

# Persistent state for sparklines and fallback
_last_good = {}
_sparkline_buffers = {
    "rtk": deque(maxlen=60),
    "headroom": deque(maxlen=60),
    "jcodemunch": deque(maxlen=60),
    "memstack": deque(maxlen=60),
}


def _run(cmd, timeout=2):
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def collect_rtk():
    """Read rtk SQLite DB and return stats + history."""
    try:
        db_path = os.path.join(HOME, ".local", "share", "rtk", "history.db")
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

        # Last 20 entries for history
        cur.execute(
            "SELECT timestamp, original_cmd, saved_tokens, savings_pct "
            "FROM commands ORDER BY id DESC LIMIT 20"
        )
        history = []
        for r in cur.fetchall():
            cmd = r["original_cmd"]
            if cmd.startswith("rtk "):
                cmd = cmd[4:]
            history.append({
                "time": r["timestamp"],
                "tool": "rtk",
                "cmd": cmd,
                "saved_pct": round(r["savings_pct"], 1),
                "saved_tokens": r["saved_tokens"],
            })

        conn.close()

        # Version
        version = _run([os.path.join(HOME, ".local", "bin", "rtk"), "--version"])
        if version:
            version = version.strip()

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


def collect_headroom():
    """Check headroom proxy stats endpoint."""
    try:
        version = _run(["headroom", "--version"])

        try:
            resp = urlopen("http://127.0.0.1:8787/stats", timeout=2)
            data = json.loads(resp.read().decode())
            data["active"] = True
            data["version"] = version or "unknown"
            return data
        except (URLError, OSError, json.JSONDecodeError):
            return {
                "active": False,
                "version": version or "unknown",
            }
    except Exception:
        return None


def collect_jcodemunch():
    """Read jcodemunch savings and index stats."""
    try:
        savings_path = os.path.join(HOME, ".code-index", "_savings.json")
        total_tokens_saved = 0
        if os.path.exists(savings_path):
            with open(savings_path) as f:
                data = json.load(f)
            total_tokens_saved = data.get("total_tokens_saved", 0)

        # Count .db files and sum sizes
        index_dir = os.path.join(HOME, ".code-index")
        db_files = glob.glob(os.path.join(index_dir, "*.db"))
        repos_indexed = len(db_files)
        index_size_bytes = sum(os.path.getsize(f) for f in db_files if os.path.exists(f))
        index_size_mb = round(index_size_bytes / (1024 * 1024), 1)

        version = _run(["jcodemunch-mcp", "--version"])

        return {
            "active": True,
            "total_saved": total_tokens_saved,
            "repos_indexed": repos_indexed,
            "index_size_mb": index_size_mb,
            "version": version or "unknown",
        }
    except Exception:
        return None


def collect_memstack():
    """Run memstack stats command and count skills."""
    try:
        db_script = os.path.join(HOME, ".claude", "skills", "db", "memstack-db.py")
        output = _run(["python3", db_script, "stats"])
        if output:
            stats = json.loads(output)
        else:
            stats = {}

        # Count .md files in skills directory
        skills_dir = os.path.join(HOME, ".claude", "skills", "skills")
        skills_count = 0
        if os.path.isdir(skills_dir):
            for _root, _dirs, files in os.walk(skills_dir):
                skills_count += sum(1 for f in files if f.endswith(".md"))

        version = _run(
            ["git", "-C", os.path.join(HOME, ".claude", "skills"),
             "describe", "--tags", "--always"]
        )

        return {
            "active": True,
            "sessions": stats.get("sessions", 0),
            "insights": stats.get("insights", 0),
            "projects": stats.get("projects", 0),
            "skills": skills_count,
            "db_size_kb": stats.get("db_size_kb", 0),
            "version": version or "unknown",
        }
    except Exception:
        return None


def collect_all():
    """Collect from all tools, maintain sparklines and fallbacks."""
    global _last_good

    collectors = {
        "rtk": collect_rtk,
        "headroom": collect_headroom,
        "jcodemunch": collect_jcodemunch,
        "memstack": collect_memstack,
    }

    results = {}
    for name, fn in collectors.items():
        data = fn()
        if data is not None:
            results[name] = data
            _last_good[name] = data
        else:
            results[name] = _last_good.get(name, {"active": False, "version": "unknown"})

    # Build combined saved total
    combined_saved = 0
    for name in collectors:
        tool_data = results[name]
        combined_saved += tool_data.get("total_saved", 0)

    # Update sparkline buffers with cumulative totals
    now = time.time()
    for name in collectors:
        tool_data = results[name]
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

    # Sort by time descending, limit to 20
    history.sort(key=lambda x: x.get("time", ""), reverse=True)
    history = history[:20]

    timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "timestamp": timestamp,
        "combined_saved": combined_saved,
        "rtk": results["rtk"],
        "headroom": results["headroom"],
        "jcodemunch": results["jcodemunch"],
        "memstack": results["memstack"],
        "sparklines": sparklines,
        "history": history,
    }


# --- HTML Frontend ---

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Tools Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #0a0a1a;
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Courier New', monospace;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    color: #ccc;
    padding: 24px;
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
.header-centre {
    color: #888;
    font-size: 13px;
}
.header-centre span {
    color: #00ff88;
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
}
.card-name {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 2px;
    font-weight: bold;
    color: #999;
}
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

/* Tool colours */
.clr-rtk { color: #00ff88; }
.fill-rtk { background: #00ff88; }
.stroke-rtk { stroke: #00ff88; }
.area-rtk { fill: rgba(0, 255, 136, 0.1); }

.clr-headroom { color: #00bfff; }
.fill-headroom { background: #00bfff; }
.stroke-headroom { stroke: #00bfff; }
.area-headroom { fill: rgba(0, 191, 255, 0.1); }

.clr-jcodemunch { color: #ff9f43; }
.fill-jcodemunch { background: #ff9f43; }
.stroke-jcodemunch { stroke: #ff9f43; }
.area-jcodemunch { fill: rgba(255, 159, 67, 0.1); }

.clr-memstack { color: #a855f7; }
.fill-memstack { background: #a855f7; }
.stroke-memstack { stroke: #a855f7; }
.area-memstack { fill: rgba(168, 85, 247, 0.1); }

/* Feed */
.feed-container {
    background: #111;
    border: 1px solid #1a1a2e;
    border-radius: 4px;
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
    height: 220px;
    overflow: hidden;
    font-size: 13px;
    line-height: 2.1;
    padding: 10px 16px;
}
.feed-line {
    display: flex;
    gap: 16px;
    align-items: center;
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
    <div class="header-left">
        <div class="pulse-dot"></div>
        <div class="header-title">CLAUDE TOOLS</div>
    </div>
    <div class="header-centre" id="combined">COMBINED: <span>0</span> tokens saved</div>
    <div class="header-right" id="clock">--:--:-- &blacksquare; -- --- ----</div>
</div>

<!-- Cards -->
<div class="cards">
    <!-- RTK -->
    <div class="card" id="rtk-card">
        <div class="card-header">
            <span class="card-name">RTK</span>
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
            <span class="card-name">Headroom</span>
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
            <span class="card-name">jCodeMunch</span>
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

    <!-- MemStack -->
    <div class="card" id="memstack-card">
        <div class="card-header">
            <span class="card-name">MemStack</span>
            <span class="card-version" id="memstack-version">--</span>
        </div>
        <div class="card-value clr-memstack" id="memstack-value">--</div>
        <div class="card-sub" id="memstack-sub">sessions tracked</div>
        <div class="progress-track"><div class="progress-fill fill-memstack" id="memstack-bar" style="width:0%"></div></div>
        <div class="card-stats" id="memstack-stats">
            <span><span class="label">skills</span> <span class="val">--</span></span>
        </div>
        <div class="sparkline-container"><svg id="memstack-sparkline" viewBox="0 0 200 35" preserveAspectRatio="none"></svg></div>
        <div class="card-delta" id="memstack-delta"></div>
    </div>
</div>

<!-- Activity Feed -->
<div class="feed-container">
    <div class="feed-header">
        <span class="feed-title">LIVE ACTIVITY</span>
        <span class="feed-count">showing last 20</span>
    </div>
    <div class="feed-area" id="feed"></div>
</div>

<script>
function formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
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

var TOOLS = ['rtk', 'headroom', 'jcodemunch', 'memstack'];
var TOOL_COLOURS = {
    rtk: '#00ff88',
    headroom: '#00bfff',
    jcodemunch: '#ff9f43',
    memstack: '#a855f7'
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
    // Combined
    document.getElementById('combined').innerHTML =
        'COMBINED: <span>' + formatTokens(d.combined_saved) + '</span> tokens saved';

    // RTK
    var rtk = d.rtk || {};
    var rtkCard = document.getElementById('rtk-card');
    rtkCard.className = rtk.active ? 'card' : 'card inactive';
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
    document.getElementById('headroom-version').textContent = shortVersion(hr.version);
    if (hr.active) {
        document.getElementById('headroom-value').textContent = formatTokens(hr.total_saved || 0);
        document.getElementById('headroom-sub').textContent = 'tokens saved';
        document.getElementById('headroom-bar').style.width = (hr.avg_savings_pct || 0) + '%';
        document.getElementById('headroom-stats').innerHTML =
            '<span><span class="label">sessions</span> <span class="val">' + (hr.sessions || 0) + '</span></span>';
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
    document.getElementById('jcodemunch-version').textContent = shortVersion(jc.version);
    document.getElementById('jcodemunch-value').textContent = jc.active ? formatTokens(jc.total_saved || 0) : '--';
    document.getElementById('jcodemunch-sub').textContent = 'tokens saved';
    document.getElementById('jcodemunch-bar').style.width = '0%';
    if (jc.active) {
        document.getElementById('jcodemunch-stats').innerHTML =
            '<span><span class="label">repos</span> <span class="val">' + (jc.repos_indexed || 0) + '</span></span>' +
            '<span><span class="label">indexed</span> <span class="val">' + (jc.index_size_mb || 0) + 'MB</span></span>';
    }

    // MemStack
    var ms = d.memstack || {};
    var msCard = document.getElementById('memstack-card');
    msCard.className = ms.active ? 'card' : 'card inactive';
    document.getElementById('memstack-version').textContent = shortVersion(ms.version);
    document.getElementById('memstack-value').textContent = ms.active ? (ms.sessions || 0) : '--';
    document.getElementById('memstack-sub').textContent = 'sessions tracked';
    document.getElementById('memstack-bar').style.width = '0%';
    if (ms.active) {
        document.getElementById('memstack-stats').innerHTML =
            '<span><span class="label">skills</span> <span class="val">' + (ms.skills || 0) + '</span></span>' +
            '<span><span class="label">db</span> <span class="val">' + (ms.db_size_kb || 0) + 'KB</span></span>';
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
    for (var j = 0; j < hist.length && j < 20; j++) {
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
        lines.push(
            '<div class="feed-line">' +
            '<span class="feed-time">' + shortTime(h.time) + '</span>' +
            '<span class="feed-tool" style="color:' + toolClr + '">' + (h.tool || '') + '</span>' +
            '<span class="feed-cmd">' + escHtml(h.cmd || '') + '</span>' +
            '<span class="feed-savings ' + savingsClass + '">' + savingsText + '</span>' +
            '</div>'
        );
    }
    feedEl.innerHTML = lines.join('');
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
    document.getElementById('clock').textContent = h + ':' + m + ':' + s + ' \\u25AA ' + day + ' ' + mon + ' ' + yr;
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
    console.warn('SSE connection lost, will retry...');
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


@app.route("/events")
def events():
    def stream():
        while True:
            payload = collect_all()
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(30)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=62891, debug=False, threaded=True)
