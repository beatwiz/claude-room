# Drop jcodemunch/jdocmunch, merge Usage card — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove jcodemunch-mcp and jdocmunch-mcp end-to-end (binaries, global config, project code, tests, docs). Collapse the dashboard into a single row of four cards: Combined, Usage (new merged Claude/Extra toggle), RTK, Headroom.

**Architecture:** Two commits on a new `chore/drop-jcode-jdoc` branch off `main`. Commit 1 is a pure removal — backend collectors, flat-contract keys, tests, README, CHANGELOG, permissions, binaries. After commit 1 the dashboard still renders (two cards in a row + two summary cards above). Commit 2 rewrites the HTML/CSS/JS to merge Claude+Extra into one Usage card and put all four cards in one grid. Backend data contract for the Usage card is unchanged — the toggle is purely client-side and reads the flat `extra_usage_*` / `session_pct` / `weekly_pct` / `sonnet_pct` fields that already exist.

**Tech Stack:** Python 3.14, Flask, vanilla JS, SSE, pytest. Single-file `app.py` contains backend + inlined HTML template.

**Spec:** `docs/superpowers/specs/2026-04-15-drop-jcode-jdoc-merge-usage-design.md`

---

## File Structure

**Files modified:**

- `app.py` — collectors, state, flat contract, HTML/CSS/JS template.
- `tests/test_app.py` — contract keys, fixtures, assertions, tool iterations.
- `README.md` — intro sentence + env vars table.
- `CHANGELOG.md` — new Unreleased entry.
- `.claude/settings.local.json` — permissions.
- `~/.claude/settings.json` — one bash permission.

**Files deleted:**

- `~/.claude/CLAUDE.md.bak` (verify content first).
- `~/.local/bin/jcodemunch-mcp`, `~/.local/bin/jdocmunch-mcp` (via uninstall, fallback to `rm`).

**Files not touched:**

- `docs/superpowers/specs/2026-04-13-*.md`, `docs/superpowers/plans/2026-04-13-*.md` — historical.
- `~/.claude/history.jsonl`, `shell-snapshots/*`, `paste-cache/*`, `settings.json.bak` — ephemeral/backup logs.

---

# Phase A — Branch + binary/global cleanup

### Task A1: Create branch

**Files:** none

- [ ] **Step 1: Create and switch to new branch**

Run:
```bash
cd ~/Work/claude-tools-dashboard
git checkout main
git pull --ff-only
git checkout -b chore/drop-jcode-jdoc
```
Expected: branch `chore/drop-jcode-jdoc` checked out.

---

### Task A2: Check for lingering shell references

**Files:** none (read-only)

- [ ] **Step 1: Grep shell rc files and crontab for jcode/jdoc refs**

Run:
```bash
for f in ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile; do
  [ -f "$f" ] && grep -Hn "jcodemunch\|jdocmunch" "$f" || true
done
crontab -l 2>/dev/null | grep -n "jcodemunch\|jdocmunch" || true
ls ~/Library/LaunchAgents 2>/dev/null | grep -i "jcodemunch\|jdocmunch" || true
```
Expected: no matches. If any match is found, STOP and report to the user — do not proceed with binary removal until they confirm.

---

### Task A3: Determine binary install source

**Files:** none (read-only)

- [ ] **Step 1: Probe pipx, pip, file**

Run:
```bash
pipx list --short 2>/dev/null | grep -E "jcodemunch|jdocmunch" || echo "pipx: none"
/Users/gsilva/venv/bin/pip show jcodemunch-mcp 2>/dev/null | head -2 || true
/Users/gsilva/venv/bin/pip show jdocmunch-mcp 2>/dev/null | head -2 || true
python3 -m pip show jcodemunch-mcp 2>/dev/null | head -2 || true
python3 -m pip show jdocmunch-mcp 2>/dev/null | head -2 || true
file ~/.local/bin/jcodemunch-mcp ~/.local/bin/jdocmunch-mcp
head -1 ~/.local/bin/jcodemunch-mcp ~/.local/bin/jdocmunch-mcp
```
Expected: the shebang line or `file` output reveals whether the binary is a pipx shim, a venv script, or a standalone binary. Record the install method for Task A4.

---

### Task A4: Uninstall the binaries

**Files:** `~/.local/bin/jcodemunch-mcp`, `~/.local/bin/jdocmunch-mcp`

- [ ] **Step 1: Uninstall via matching tool; fall back to rm**

Pick the command for your install method from Task A3:

- If `pipx`:
  ```bash
  pipx uninstall jcodemunch-mcp
  pipx uninstall jdocmunch-mcp
  ```
- If `/Users/gsilva/venv/bin/pip`:
  ```bash
  /Users/gsilva/venv/bin/pip uninstall -y jcodemunch-mcp jdocmunch-mcp
  ```
- If standalone (`file` said `Mach-O executable` or the shebang points to a one-off wrapper):
  ```bash
  rm ~/.local/bin/jcodemunch-mcp ~/.local/bin/jdocmunch-mcp
  ```

- [ ] **Step 2: Verify binaries are gone**

Run:
```bash
ls ~/.local/bin/jcodemunch-mcp ~/.local/bin/jdocmunch-mcp 2>&1 || echo "ok: both removed"
which jcodemunch-mcp jdocmunch-mcp 2>&1 || echo "ok: not on PATH"
```
Expected: `ls` reports `No such file`. `which` reports not found.

---

### Task A5: Scrub global settings.json permission

**Files:** `~/.claude/settings.json`

- [ ] **Step 1: Remove the jcodemunch-mcp permission line**

Read the file first to verify the exact line (expected near line 135: `"jcodemunch-mcp *",`). Then use Edit to remove that single array entry, preserving JSON validity.

- [ ] **Step 2: Verify JSON still parses**

Run:
```bash
python3 -c "import json; json.load(open('/Users/gsilva/.claude/settings.json')); print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Verify no remaining references**

Run:
```bash
grep -n "jcodemunch\|jdocmunch" ~/.claude/settings.json || echo "clean"
```
Expected: `clean`.

---

### Task A6: Delete CLAUDE.md.bak

**Files:** `~/.claude/CLAUDE.md.bak`

- [ ] **Step 1: Verify the file contains only jcodemunch documentation**

Run:
```bash
grep -v "jcodemunch\|jdocmunch" ~/.claude/CLAUDE.md.bak | head -40
```
If the output shows meaningful non-jcode content, STOP and report to the user. If it's only headings and empty lines, continue.

- [ ] **Step 2: Delete the file**

Run:
```bash
rm ~/.claude/CLAUDE.md.bak
ls ~/.claude/CLAUDE.md.bak 2>&1 || echo "ok: removed"
```
Expected: `ok: removed`.

---

# Phase B — Gut backend

### Task B1: Establish pytest baseline

**Files:** none

- [ ] **Step 1: Run the test suite before touching code**

Run:
```bash
cd ~/Work/claude-tools-dashboard
pytest -q 2>&1 | tail -20
```
Expected: all tests pass on `main` HEAD. Record the passing test count.

---

### Task B2: Remove env vars and module-level state

**Files:** Modify `app.py:29-85`

- [ ] **Step 1: Drop JCODEMUNCH_* and JDOCMUNCH_* env vars**

Edit `app.py` — delete these four lines (29–32):

```python
JCODEMUNCH_INDEX_DIR = os.environ.get("JCODEMUNCH_INDEX_DIR", os.path.join(HOME, ".code-index"))
JCODEMUNCH_BIN = os.environ.get("JCODEMUNCH_BIN", "jcodemunch-mcp")
JDOCMUNCH_INDEX_DIR = os.environ.get("JDOCMUNCH_INDEX_DIR", os.path.join(HOME, ".doc-index"))
JDOCMUNCH_BIN = os.environ.get("JDOCMUNCH_BIN", "jdocmunch-mcp")
```

- [ ] **Step 2: Drop sparkline buffers and state globals**

Edit the `_sparkline_buffers` dict to drop the two keys. Before:

```python
_sparkline_buffers = {
    "rtk": deque(maxlen=240),
    "headroom": deque(maxlen=240),
    "jcodemunch": deque(maxlen=240),
    "jdocmunch": deque(maxlen=240),
}
```

After:

```python
_sparkline_buffers = {
    "rtk": deque(maxlen=240),
    "headroom": deque(maxlen=240),
}
```

Then delete these six global-state lines (65–70):

```python
_jcodemunch_last_total = 0
_jcodemunch_last_mtime = 0
_jcodemunch_history = []
_jdocmunch_last_total = 0
_jdocmunch_last_mtime = 0
_jdocmunch_history = []
```

Edit the `_last_collect_success` dict to drop the two keys. Before:

```python
_last_collect_success = {
    "rtk": 0.0,
    "headroom": 0.0,
    "jcodemunch": 0.0,
    "jdocmunch": 0.0,
}
```

After:

```python
_last_collect_success = {
    "rtk": 0.0,
    "headroom": 0.0,
}
```

Edit the `_cached_versions` dict to drop the two keys. Before:

```python
_cached_versions = {
    "rtk": None,
    "jcodemunch": None,
    "jdocmunch": None,
}
```

After:

```python
_cached_versions = {
    "rtk": None,
}
```

---

### Task B3: Remove version readers

**Files:** Modify `app.py:88-120`

- [ ] **Step 1: Delete _read_jcodemunch_config_version helper**

Remove the entire function and the trailing blank line:

```python
def _read_jcodemunch_config_version():
    config_path = os.path.join(JCODEMUNCH_INDEX_DIR, "config.jsonc")
    try:
        with open(config_path) as f:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', f.read())
            return m.group(1) if m else None
    except OSError:
        return None
```

- [ ] **Step 2: Trim resolve_versions_once to rtk only**

Before (lines 98–120):

```python
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
```

After:

```python
def resolve_versions_once():
    """Resolve tool versions once. Called by DashboardCollector.run() at thread startup.
    Per-tool precedence: TOOL_VERSION env var > binary --version > "unknown"."""
    rtk_v = os.environ.get("RTK_VERSION") or _run([RTK_BIN, "--version"])
    _cached_versions["rtk"] = rtk_v if rtk_v else "unknown"
```

---

### Task B4: Delete collect_jcodemunch and collect_jdocmunch

**Files:** Modify `app.py:390-556`

- [ ] **Step 1: Delete the two collector functions**

Remove the entire block from `def collect_jcodemunch():` (line ~390) through the closing `return None` / blank lines of `collect_jdocmunch` (line ~556). This is roughly 167 lines of contiguous code. Verify the line immediately before is the blank line after `collect_headroom`'s `return None` in its except block, and the line immediately after is `def collect_claude_usage(stats_raw=None):`.

- [ ] **Step 2: Verify no stragglers**

Run:
```bash
grep -n "_jcodemunch_\|_jdocmunch_\|collect_jcodemunch\|collect_jdocmunch\|_read_jcodemunch" app.py || echo "clean"
```
Expected: `clean`.

---

### Task B5: Trim collect_all

**Files:** Modify `app.py:996-1177`

- [ ] **Step 1: Update the collectors dict**

Before:

```python
    collectors = {
        "rtk": collect_rtk,
        "headroom": lambda: collect_headroom(stats_raw=headroom_stats_raw),
        "jcodemunch": collect_jcodemunch,
        "jdocmunch": collect_jdocmunch,
    }
```

After:

```python
    collectors = {
        "rtk": collect_rtk,
        "headroom": lambda: collect_headroom(stats_raw=headroom_stats_raw),
    }
```

- [ ] **Step 2: Trim the per-tool history merge**

Before:

```python
    history = []
    if "history" in results.get("rtk", {}):
        history.extend(results["rtk"]["history"])
    if "history" in results.get("headroom", {}):
        history.extend(results["headroom"]["history"])
    if "history" in results.get("jcodemunch", {}):
        history.extend(results["jcodemunch"]["history"])
    if "history" in results.get("jdocmunch", {}):
        history.extend(results["jdocmunch"]["history"])
```

After:

```python
    history = []
    if "history" in results.get("rtk", {}):
        history.extend(results["rtk"]["history"])
    if "history" in results.get("headroom", {}):
        history.extend(results["headroom"]["history"])
```

- [ ] **Step 3: Trim the per-tool history-pop loop**

Before:

```python
    for tool_name in ("rtk", "headroom", "jcodemunch", "jdocmunch"):
        trimmed = dict(results[tool_name])
        trimmed.pop("history", None)
        results[tool_name] = trimmed
```

After:

```python
    for tool_name in ("rtk", "headroom"):
        trimmed = dict(results[tool_name])
        trimmed.pop("history", None)
        results[tool_name] = trimmed
```

- [ ] **Step 4: Trim the return dict**

Before:

```python
    return {
        "ready": True,
        "timestamp": timestamp,
        "combined_saved": combined_saved,
        "rtk": results["rtk"],
        "headroom": results["headroom"],
        "jcodemunch": results["jcodemunch"],
        "jdocmunch": results["jdocmunch"],
        "sparklines": sparklines,
        ...
    }
```

After:

```python
    return {
        "ready": True,
        "timestamp": timestamp,
        "combined_saved": combined_saved,
        "rtk": results["rtk"],
        "headroom": results["headroom"],
        "sparklines": sparklines,
        ...
    }
```

(Leave the other keys — `history`, `claude_usage`, `weekly` — untouched.)

---

### Task B6: Strip jcm/jdm from _flatten_snapshot

**Files:** Modify `app.py:835-982`

- [ ] **Step 1: Drop the local variable reads**

Find and delete these lines inside `_flatten_snapshot`:

```python
    jcm = snap.get("jcodemunch") or {}
    jdm = snap.get("jdocmunch") or {}
```

and:

```python
    spark_jcm = sparklines.get("jcodemunch") or {}
    spark_jdm = sparklines.get("jdocmunch") or {}
```

- [ ] **Step 2: Update the combined_saved_usd non-headroom-tokens sum**

Before:

```python
    combined_saved_usd = None
    if usd_per_token is not None:
        non_headroom_tokens = (
            (rtk.get("total_saved") or 0)
            + (jcm.get("total_saved") or 0)
            + (jdm.get("total_saved") or 0)
        )
        combined_saved_usd = hr_lifetime_usd + non_headroom_tokens * usd_per_token
```

After:

```python
    combined_saved_usd = None
    if usd_per_token is not None:
        non_headroom_tokens = rtk.get("total_saved") or 0
        combined_saved_usd = hr_lifetime_usd + non_headroom_tokens * usd_per_token
```

- [ ] **Step 3: Drop the 18 jcm/jdm flat-contract fields**

Delete these two blocks from the return dict:

```python
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
```

- [ ] **Step 4: Verify app.py has no remaining jcode/jdoc refs outside HTML**

Run:
```bash
grep -n "jcodemunch\|jdocmunch" app.py | grep -v "<!--\|github.com/jgravelle\|clr-\|fill-\|stroke-\|area-\|jcodemunch-card\|jdocmunch-card\|jcodemunch-\|jdocmunch-\|'jcodemunch'\|'jdocmunch'\|jc = d\|jd = d\|jc\\.\|jd\\."
```
Expected: no matches (only HTML/CSS/JS refs remain, and those will be addressed in Phase C).

More reliable check — only look above the `HTML = """` line:

```bash
awk '/^HTML = """/{exit} {print NR": "$0}' app.py | grep -n "jcodemunch\|jdocmunch" || echo "backend clean"
```
Expected: `backend clean`.

---

### Task B7: Update test CONTRACT_KEYS and FULL_SNAP

**Files:** Modify `tests/test_app.py:19-85, 161-237`

- [ ] **Step 1: Remove the 18 jcm/jdm keys from CONTRACT_KEYS**

Delete lines 57–74 (the `"jcodemunch_active"` through `"jdocmunch_freshness_label"` block).

- [ ] **Step 2: Remove jcodemunch and jdocmunch sub-dicts from FULL_SNAP**

Delete these two blocks from the `FULL_SNAP` fixture:

```python
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
```

- [ ] **Step 3: Remove jcm/jdm sparkline entries from FULL_SNAP**

Before:

```python
    "sparklines": {
        "rtk": {"delta": 42, "points": []},
        "headroom": {"delta": 10, "points": []},
        "jcodemunch": {"delta": 0, "points": []},
        "jdocmunch": {"delta": 0, "points": []},
    },
```

After:

```python
    "sparklines": {
        "rtk": {"delta": 42, "points": []},
        "headroom": {"delta": 10, "points": []},
    },
```

- [ ] **Step 4: Update the combined_saved_usd assertion**

The old assertion was:

```python
    assert flat["combined_saved_usd"] == pytest.approx(584.41 + 83456 * (584.41 / 117309038))
```

`83456 = 50000 + 20000 + 13456` was rtk + jcm + jdm. With jcm+jdm removed, only rtk (50000) remains. Replace with:

```python
    assert flat["combined_saved_usd"] == pytest.approx(584.41 + 50000 * (584.41 / 117309038))
```

---

### Task B8: Trim tool iteration and assertion blocks in test_app.py

**Files:** Modify `tests/test_app.py:120-149, 287-307, 352-363`

- [ ] **Step 1: Shrink the tool iteration tuple**

Before:

```python
    for tool in ("rtk", "headroom", "jcodemunch", "jdocmunch"):
        assert flat[f"{tool}_active"] is False
        assert flat[f"{tool}_health"] == "error"
        assert flat[f"{tool}_version"] == "unknown"
        assert flat[f"{tool}_saved"] == 0
        assert flat[f"{tool}_delta"] == 0
```

After:

```python
    for tool in ("rtk", "headroom"):
        assert flat[f"{tool}_active"] is False
        assert flat[f"{tool}_health"] == "error"
        assert flat[f"{tool}_version"] == "unknown"
        assert flat[f"{tool}_saved"] == 0
        assert flat[f"{tool}_delta"] == 0
```

- [ ] **Step 2: Delete the 8 jcm/jdm default assertions in test_flatten_snapshot_none_returns_ready_false**

Delete lines 142–149:

```python
    assert flat["jcodemunch_repos_indexed"] == 0
    assert flat["jcodemunch_index_size_mb"] == 0
    assert flat["jcodemunch_freshness"] == 0
    assert flat["jcodemunch_freshness_label"] == "idle"
    assert flat["jdocmunch_docs_indexed"] == 0
    assert flat["jdocmunch_index_size_mb"] == 0
    assert flat["jdocmunch_freshness"] == 0
    assert flat["jdocmunch_freshness_label"] == "idle"
```

- [ ] **Step 3: Delete the 18 jcm/jdm assertions in test_flatten_snapshot_full_payload**

Delete lines 287–307 (the `# jcodemunch` and `# jdocmunch` comment blocks and their assertions).

- [ ] **Step 4: Delete the 2 jcm/jdm delta assertions in test_flatten_snapshot_missing_sparklines**

Before:

```python
    assert flat["rtk_delta"] == 0
    assert flat["headroom_delta"] == 0
    assert flat["jcodemunch_delta"] == 0
    assert flat["jdocmunch_delta"] == 0
    # Other rtk fields still work
    assert flat["rtk_saved"] == 50000
```

After:

```python
    assert flat["rtk_delta"] == 0
    assert flat["headroom_delta"] == 0
    # Other rtk fields still work
    assert flat["rtk_saved"] == 50000
```

- [ ] **Step 5: Update test_status_route_happy_path final assertion**

The line currently asserts `body["jcodemunch_freshness_label"] == "3m ago"`. Replace with a different live field from the fixture:

Before:

```python
    assert body["jcodemunch_freshness_label"] == "3m ago"
```

After:

```python
    assert body["headroom_lifetime_saved"] == 117309038
```

- [ ] **Step 6: Strip jcm/jdm from the snapshot in test_flatten_snapshot_no_usd_when_headroom_usd_missing**

Delete these two lines from the snap literal:

```python
        "jcodemunch": {"active": False, "health": "error", "version": "unknown", "total_saved": 0},
        "jdocmunch": {"active": False, "health": "error", "version": "unknown", "total_saved": 0},
```

---

### Task B9: Add contract-purity assertion

**Files:** Modify `tests/test_app.py` (append near existing contract tests, e.g. after `test_flatten_snapshot_none_returns_ready_false`)

- [ ] **Step 1: Write a failing test for zero jcm/jdm contract keys**

Add this new test function directly after `test_flatten_snapshot_none_returns_ready_false`:

```python
def test_flatten_snapshot_has_no_jcode_jdoc_keys():
    """Regression guard: the flat contract must not leak any jcodemunch_*
    or jdocmunch_* keys after the removal refactor."""
    import app

    flat = app._flatten_snapshot(None)
    leaked = [k for k in flat.keys() if k.startswith("jcodemunch_") or k.startswith("jdocmunch_")]
    assert leaked == [], f"flat contract still exposes removed keys: {leaked}"
```

- [ ] **Step 2: Verify the test is green (it should pass since Task B6 already removed the fields)**

Run:
```bash
pytest tests/test_app.py::test_flatten_snapshot_has_no_jcode_jdoc_keys -v
```
Expected: PASS.

---

### Task B10: Run the full backend test suite

**Files:** none

- [ ] **Step 1: Run pytest**

Run:
```bash
pytest -q 2>&1 | tail -30
```
Expected: all tests pass. If any fail, fix them inline before proceeding. Note that Task B1 recorded the baseline passing count — the new count should equal `baseline - (tests removed in this phase) + 1` (the new contract-purity test). If it's less than that, investigate.

- [ ] **Step 2: Final backend grep**

Run:
```bash
awk '/^HTML = """/{exit} {print}' app.py | grep -n "jcodemunch\|jdocmunch\|JCODEMUNCH\|JDOCMUNCH" || echo "backend clean"
grep -n "jcodemunch\|jdocmunch\|JCODEMUNCH\|JDOCMUNCH" tests/test_app.py || echo "tests clean"
```
Expected: `backend clean` and `tests clean`.

---

### Task B11: Update README.md

**Files:** Modify `README.md`

- [ ] **Step 1: Rewrite the intro sentence**

The README intro references all four tools. Rewrite line ~8 to name only RTK and Headroom:

Read the current line first; you should see something like:

```
Live wallboard for monitoring token savings across your Claude Code toolchain...
```

If the sentence enumerates rtk/headroom/jcodemunch/jdocmunch specifically, rewrite it as:

```
Live wallboard for monitoring token savings across your Claude Code toolchain — RTK and Headroom.
```

If the sentence is generic, leave it. The goal is to delete any literal `jCodeMunch` / `jDocMunch` product names from the intro.

- [ ] **Step 2: Drop the JCODEMUNCH_BIN env-var row**

Find the table row:

```
| `JCODEMUNCH_BIN` | `jcodemunch-mcp` | Path to jCodeMunch binary |
```

Delete that row. If a `JDOCMUNCH_BIN` or `JCODEMUNCH_INDEX_DIR` / `JDOCMUNCH_INDEX_DIR` row is also present, delete those too.

- [ ] **Step 3: Verify README clean**

Run:
```bash
grep -n "jcodemunch\|jdocmunch\|JCODEMUNCH\|JDOCMUNCH\|jCodeMunch\|jDocMunch" README.md || echo "clean"
```
Expected: `clean`.

---

### Task B12: Update .env.example

**Files:** Modify `.env.example`

- [ ] **Step 1: Drop jcode/jdoc lines**

Run:
```bash
grep -n "JCODEMUNCH\|JDOCMUNCH" .env.example
```

If any lines match, delete them via Edit. Then verify:

```bash
grep -n "JCODEMUNCH\|JDOCMUNCH" .env.example || echo "clean"
```
Expected: `clean`.

---

### Task B13: Update CHANGELOG.md

**Files:** Modify `CHANGELOG.md`

- [ ] **Step 1: Add a new Unreleased entry at the top**

Insert a new section immediately under the title line (before the first existing version section):

```markdown
## [Unreleased]

### Removed
- Dropped jcodemunch-mcp and jdocmunch-mcp integration end-to-end: collectors, flat-contract keys, env vars, dashboard cards, and global config permissions. The `JCODEMUNCH_*` / `JDOCMUNCH_*` env vars and the `jcodemunch_*` / `jdocmunch_*` flat JSON fields are gone. `/api/status` consumers that still read those keys must be updated.

### Changed
- Merged the Claude Usage and Extra Usage cards into a single **Usage** card with a `[Claude | Extra]` toggle. Default mode auto-follows `extra_usage_enabled`; a user click sticks for the session.
- Dashboard collapses to a single row of four cards: Combined, Usage, RTK, Headroom.
```

---

### Task B14: Update .claude/settings.local.json

**Files:** Modify `.claude/settings.local.json`

- [ ] **Step 1: Remove the 13 jcode/jdoc permissions**

Read the file, locate the `"permissions"` → `"allow"` array, delete every entry that matches `mcp__jcodemunch__*` or `Bash(jdocmunch-mcp --version)`. Keep the surrounding JSON structure intact.

- [ ] **Step 2: Verify JSON validity and zero refs**

Run:
```bash
python3 -c "import json; json.load(open('.claude/settings.local.json')); print('ok')"
grep -n "jcodemunch\|jdocmunch" .claude/settings.local.json || echo "clean"
```
Expected: `ok` and `clean`.

---

### Task B15: Commit Phase B

**Files:** none

- [ ] **Step 1: Stage and commit**

Run:
```bash
cd ~/Work/claude-tools-dashboard
git add -A
git status
```

Review `git status` to confirm only the expected files changed: `app.py`, `tests/test_app.py`, `README.md`, `.env.example`, `CHANGELOG.md`, `.claude/settings.local.json`. If anything else appears, investigate before committing.

```bash
git commit -m "$(cat <<'EOF'
chore: drop jcodemunch/jdocmunch collectors, cards data, permissions

Remove the two MCP tool integrations end-to-end — collectors, state,
flat-contract fields, tests, README, env vars, and .claude permissions.
Dashboard UI still renders the summary + two remaining tool cards; the
layout rework follows in the next commit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit lands on `chore/drop-jcode-jdoc`.

- [ ] **Step 2: Rebuild + smoke-test the container**

Run:
```bash
./build.sh 2>&1 | tail -20
```
Expected: container rebuilds and restarts without error.

Then:

```bash
curl -s http://127.0.0.1:8095/health
curl -s http://127.0.0.1:8095/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); print('ready=',d['ready']); print('has_jcm=', any(k.startswith('jcodemunch_') for k in d)); print('has_jdm=', any(k.startswith('jdocmunch_') for k in d))"
```
Expected: `{"status": "ok"}`, `ready= True`, `has_jcm= False`, `has_jdm= False`.

---

# Phase C — UI merge and single-row layout

### Task C1: Collapse the two grids into one

**Files:** Modify `app.py` (CSS block around lines 1285–1625)

- [ ] **Step 1: Update the .cards grid rule to host 4 columns**

The existing rule at line ~1285:

```css
.cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 12px;
}
```

Leave it exactly as-is (4 columns is the new target).

- [ ] **Step 2: Delete the entire .summary-cards CSS block**

Delete everything from the `/* Summary cards */` comment through the closing brace of the `@media (max-width: 600px)` block that contains `.summary-cards`-prefixed selectors. This is roughly lines 1504–1625. After deletion, the next CSS block should flow into the end-of-style (`</style>` tag).

Important: keep the generic `@media (max-width: 1000px) { .cards { grid-template-columns: repeat(2, 1fr); } }` and `@media (max-width: 550px) { .cards { grid-template-columns: 1fr; } }` rules at lines ~1498–1503 — those already provide responsive fallback for `.cards`.

- [ ] **Step 3: Drop the jcodemunch/jdocmunch colour classes**

Delete the two blocks:

```css
.clr-jcodemunch { color: #ec4899; }
.fill-jcodemunch { background: #ec4899; }
.stroke-jcodemunch { stroke: #ec4899; }
.area-jcodemunch { fill: rgba(236, 72, 153, 0.1); }

.clr-jdocmunch { color: #14b8a6; }
.fill-jdocmunch { background: #14b8a6; }
.stroke-jdocmunch { stroke: #14b8a6; }
.area-jdocmunch { fill: rgba(20, 184, 166, 0.1); }
```

- [ ] **Step 4: Add Usage-card and Combined-card scoped CSS**

Immediately after the `.card-delta` CSS block (near line ~1361 in the original file), append the following block. These are the rules that `.summary-cards` used to provide; they now live inside `.cards` scope.

```css
/* Combined + Usage card specifics (replaces old .summary-cards rules) */
.cards .card-combined .card-value { color: #00ff88; margin-bottom: 0; }
.cards .card-combined .card-sub-usd { color: #5cc48a; font-size: 12px; margin-top: 2px; }
.cards .card-combined .combined-body {
    display: flex;
    flex-direction: column;
    gap: 14px;
}
.cards .card-combined .combined-stats {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.cards .card-combined .combined-stats .stat-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: baseline;
}
.cards .card-combined .combined-stats .label {
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-size: 10px;
}
.cards .card-combined .combined-stats .val {
    color: #fff;
    font-weight: 600;
    font-size: 14px;
}
.cards .card-combined .val-live { color: #00ff88; }
.cards .card-combined .val-rate { color: #5cc48a; }
.cards .card-combined .val-cold { color: #666; }

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
```

---

### Task C2: Rewrite the HTML card markup

**Files:** Modify `app.py` (HTML block around lines 1638–1746)

- [ ] **Step 1: Delete the .summary-cards div and the old .cards div**

Delete everything from:

```html
<!-- Summary Cards -->
<div class="summary-cards">
```

through the closing `</div>` of the old `.cards` container (the one that ends after the jdocmunch-card block, around line 1746).

- [ ] **Step 2: Insert the new unified .cards grid with four cards**

In its place, insert the following HTML (indentation matches the surrounding template):

```html
<!-- Cards -->
<div class="cards">
    <!-- Combined -->
    <div class="card card-combined" id="summary-combined">
        <div class="card-header">
            <span class="health-dot health-ok" id="summary-combined-health"></span>
            <span class="card-name">Combined</span>
        </div>
        <div class="combined-body">
            <div>
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
</div>
```

---

### Task C3: Rewrite the JS update logic

**Files:** Modify `app.py` (JS block around lines 1815–2000)

- [ ] **Step 1: Shrink TOOLS and drop jcode/jdoc colours**

Before:

```javascript
var TOOLS = ['rtk', 'headroom', 'jcodemunch', 'jdocmunch'];
var TOOL_COLOURS = {
    rtk: '#3b82f6',
    headroom: '#8b5cf6',
    jcodemunch: '#ec4899',
    jdocmunch: '#14b8a6'
};
```

After:

```javascript
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
```

- [ ] **Step 2: Remove jc/jd locals and combined-USD calculation**

In `updateDashboard(d)`, before:

```javascript
function updateDashboard(d) {
    var w = d.weekly || {};
    var cu = d.claude_usage || {};
    var rtk = d.rtk || {};
    var hr = d.headroom || {};
    var jc = d.jcodemunch || {};
    var jd = d.jdocmunch || {};

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
    var combinedUsdEl = document.getElementById('summary-combined-usd');
    if (usdPerToken != null) {
        var nonHrTokens = (rtk.total_saved || 0) + (jc.total_saved || 0) + (jd.total_saved || 0);
        var combinedUsd = hrLifetimeUsd + nonHrTokens * usdPerToken;
        combinedUsdEl.textContent = '≈ $' + combinedUsd.toFixed(2) + ' saved';
    } else {
        combinedUsdEl.textContent = '--';
    }
```

After:

```javascript
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
    var combinedUsdEl = document.getElementById('summary-combined-usd');
    if (usdPerToken != null) {
        var nonHrTokens = rtk.total_saved || 0;
        var combinedUsd = hrLifetimeUsd + nonHrTokens * usdPerToken;
        combinedUsdEl.textContent = '≈ $' + combinedUsd.toFixed(2) + ' saved';
    } else {
        combinedUsdEl.textContent = '--';
    }
```

- [ ] **Step 3: Replace the Claude + Extra card update blocks with the unified Usage card logic**

Delete the old Claude Usage and Extra Usage update sections (the blocks commented `// --- Claude Usage card ---` and `// --- Extra Usage card ---` through line ~1925). Replace with:

```javascript
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
```

- [ ] **Step 4: Delete the jCodeMunch and jDocMunch update blocks**

Delete the two blocks commented `// jCodeMunch` and `// jDocMunch` (roughly lines 1967–1999 in the original file). They contained references to `jc.*` / `jd.*` and DOM IDs that no longer exist.

- [ ] **Step 5: Wire up the usage toggle at load time**

Add a single line after `updateClock();` (just before the `// SSE` comment, near the bottom of the script):

```javascript
wireUsageToggle();
```

---

### Task C4: Rebuild and smoke-test in browser

**Files:** none

- [ ] **Step 1: Rebuild the container**

Run:
```bash
cd ~/Work/claude-tools-dashboard
./build.sh 2>&1 | tail -20
```
Expected: rebuild + restart without errors.

- [ ] **Step 2: Verify health and JSON**

Run:
```bash
curl -s http://127.0.0.1:8095/health
curl -s http://127.0.0.1:8095/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); print('ready=',d['ready'],'rtk=',d['rtk_saved'],'hr=',d['headroom_saved']); assert not any(k.startswith('jcodemunch_') or k.startswith('jdocmunch_') for k in d), 'leaked keys found'; print('clean')"
```
Expected: `{"status": "ok"}`, then `ready= True rtk= <n> hr= <n>` and `clean`.

- [ ] **Step 3: Open the dashboard in a browser and verify**

Open `http://127.0.0.1:8095/` in Chrome. Visually confirm:

1. Four cards render in a single row: Combined, Usage, RTK, Headroom.
2. Usage card header shows a `[Claude | Extra]` toggle. The active segment has the highlighted background.
3. If extra is currently enabled on your account, the card defaults to Extra mode (dollar value visible). Otherwise it defaults to Claude mode (5-Hour / Weekly / Sonnet rows).
4. Click the non-default segment — the card body flips.
5. Click back — the card body flips back.
6. If the Extra segment is disabled (because extra is not enabled), clicking it is a no-op and its text is greyed out.
7. Narrow the window below 1000px — cards collapse to 2×2. Narrow below 550px — one column.
8. The Live Activity feed still renders RTK + Headroom entries; no broken `jcodemunch`/`jdocmunch` lines.
9. Browser DevTools console shows no JS errors.

If any check fails, debug and fix before committing.

---

### Task C5: Commit Phase C

**Files:** none

- [ ] **Step 1: Stage and commit**

Run:
```bash
git add app.py
git status
```

Review — only `app.py` should be modified. Then:

```bash
git commit -m "$(cat <<'EOF'
feat: merge claude/extra usage into single card, align to one row

- Unify Combined/Usage/RTK/Headroom into a single .cards grid (4 cols).
- Merge "Claude Usage" and "Extra Usage" into a single "Usage" card with
  a [Claude | Extra] toggle. Default mode auto-follows extra_usage_enabled;
  a user click sticks for the session. Disabled segment if extra is off.
- Drop jcode/jdoc UI assets (CSS colours, card HTML, JS update blocks,
  TOOLS array entries).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit lands.

---

### Task C6: Final sweep

**Files:** none

- [ ] **Step 1: Repo-wide grep for stragglers**

Run:
```bash
grep -rn "jcodemunch\|jdocmunch\|JCODEMUNCH\|JDOCMUNCH\|jCodeMunch\|jDocMunch" \
  --include='*.py' --include='*.md' --include='*.json' --include='*.sh' \
  --include='*.yml' --include='*.yaml' --include='*.html' --include='*.css' \
  --include='*.js' . \
  | grep -v "^./docs/superpowers/specs/2026-04-13\|^./docs/superpowers/plans/2026-04-13\|^./docs/superpowers/specs/2026-04-15\|^./docs/superpowers/plans/2026-04-15\|^./CHANGELOG.md" \
  || echo "repo clean"
```
Expected: `repo clean`.

(`CHANGELOG.md` is excluded because it mentions the removed tools in historical notes and the new Unreleased entry by design. The 2026-04-13 and 2026-04-15 spec/plan files are excluded because they're historical artifacts.)

- [ ] **Step 2: Final test run**

Run:
```bash
pytest -q 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 3: Show the commit summary**

Run:
```bash
git log --oneline main..HEAD
```
Expected: exactly two commits — the `chore:` removal and the `feat:` merge.

---

## Self-Review Notes

- **Spec coverage:** Phase A covers binary/global cleanup (spec §"Binaries", §"Global"). Phase B covers all project code/test/doc removal (spec §"Project"). Phase C covers the UI merge + single-row layout (spec §"UI design" + §"Layout"). CHANGELOG + branch/commit order from spec §"Execution order" is honoured.
- **Placeholder scan:** no TBDs, no "handle edge cases", no "write tests for the above" — every code step has full code or a full command.
- **Type consistency:** `summary-usage-toggle`, `summary-usage-claude`, `summary-usage-extra`, `summary-usage-health`, `usage-row`, `card-usage`, `usageUserOverride`, `wireUsageToggle`, `applyUsageMode` — all introduced in Task C1/C2/C3 and used consistently. Existing IDs reused as-is: `summary-combined-*`, `summary-session-pct`, `summary-weekly-pct`, `summary-sonnet-pct`, `summary-claude-reset`, `summary-extra-value`, `summary-extra-detail`, `summary-extra-bar`, `rtk-*`, `headroom-*`.
- **Commit split:** Phase B ends with a working-but-two-row dashboard (commit 1). Phase C transforms that into a single-row four-card dashboard (commit 2). Each commit independently passes tests.
