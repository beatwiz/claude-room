# Drop jcodemunch/jdocmunch, merge Usage card, align to single row

**Date:** 2026-04-15
**Status:** Approved
**Branch:** `chore/drop-jcode-jdoc` (off `main`)

## Summary

Remove jcodemunch-mcp and jdocmunch-mcp from the dashboard end-to-end: uninstall the binaries, scrub active config in `~/.claude`, drop all project references, and rework the UI so the remaining tools fit in one row of four cards. Merge `Claude Usage` and `Extra Usage` into a single `Usage` card with a `[Claude | Extra]` toggle that defaults to `Extra` when extra usage is active.

## Goals

- Leave no active reference to jcodemunch/jdocmunch in code, config, docs, or permissions.
- Dashboard renders four cards — Combined, Usage, RTK, Headroom — in a single aligned row.
- Backend `collect_all` flat contract no longer emits any `jcodemunch_*`/`jdocmunch_*` keys.
- Usage card auto-switches to Extra mode when `extra_usage_enabled` is true, and lets the user flip back via the toggle for the rest of the session.
- `/statusline` JSON contract for Claude/Extra fields is unchanged — the merge is UI-only.

## Non-goals

- Rewriting git history or editing historical specs/plans that mention the removed tools.
- Scrubbing ephemeral logs (`~/.claude/history.jsonl`, `shell-snapshots/*`, `paste-cache/*`, `settings.json.bak`).
- Adding persistence for the user's toggle choice (session-scoped is enough).
- Any change to RTK or Headroom collectors.

## Removal scope

### Project (`~/Work/claude-tools-dashboard`)

- **`app.py`** — drop:
  - `JCODEMUNCH_BIN` / `JDOCMUNCH_BIN` env vars
  - `_jcodemunch_last_total`, `_jcodemunch_last_mtime`, `_jcodemunch_history`, `_jdocmunch_*` globals
  - `_read_jcodemunch_config_version()` and version-read branches for both tools
  - `collect_jcodemunch()` and `collect_jdocmunch()` functions
  - `"jcodemunch"` / `"jdocmunch"` entries from any deque dict, cached-versions dict, and savings dict
  - `collect_all` merging of jcode/jdoc histories and their result-dict entries
  - Any jcode/jdoc keys from the flat JSON contract
  - Card HTML blocks for `jcodemunch-card` and `jdocmunch-card`
  - JS tick handlers that update those cards
- **`tests/test_app.py`** — delete all `jcodemunch_*` + `jdocmunch_*` assertions (~18 lines) and remove `"jcodemunch"`/`"jdocmunch"` from the tool iteration tuple. Add a new assertion that the flat contract contains no key starting with `jcodemunch_` or `jdocmunch_`.
- **`README.md`** — drop the `JCODEMUNCH_BIN` env row and rewrite the intro sentence so it no longer names the removed tools.
- **`CHANGELOG.md`** — add a new `## [Unreleased]` section recording the removal and the UI merge. **Do not** edit past entries.
- **`.claude/settings.local.json`** — remove the 12 `mcp__jcodemunch__*` permissions and the `Bash(jdocmunch-mcp --version)` permission.
- **`docs/superpowers/specs/2026-04-13-*.md` / `plans/2026-04-13-*.md`** — leave as-is (historical record).

### Global (`~/.claude`)

- **`settings.json`** — delete the `"jcodemunch-mcp *"` bash permission (line ~135).
- **`CLAUDE.md.bak`** — verify the file contains only jcodemunch documentation, then delete it.
- **Skipped:** `history.jsonl`, `shell-snapshots/*`, `paste-cache/*`, `settings.json.bak` — ephemeral / backup logs.

### Binaries

- `~/.local/bin/jcodemunch-mcp`, `~/.local/bin/jdocmunch-mcp`
- Detect install source first: try `pipx list`, then `pip show jcodemunch-mcp jdocmunch-mcp`, then `file` on the binary.
- Uninstall via the matching tool. Fall back to `rm` only if standalone.
- Before removing: grep `~/.zshrc`, `~/.zprofile`, `~/.bashrc`, crontab, launchd agents for any references. Warn the user if any are found.

## UI design: merged Usage card

### Structure

Replace `#summary-claude` and `#summary-extra` with a single card `#summary-usage`:

```
┌───────────────────────┐
│ Usage    [Claude|Extra]│
│     72%               │
│ week · opus           │
└───────────────────────┘
```

- Header: `Usage` title + segmented pill toggle on the right.
- Body: the existing Claude-usage or Extra-usage render block, picked by current mode.
- Footer sub-line: unchanged per mode (model name / window for Claude; dollars / limit for Extra).

### Mode-selection logic (client-side, no new backend fields)

```
default_mode = "extra" if flat.extra_usage_enabled else "claude"
mode = user_override ?? default_mode
```

- `user_override` starts `null`. When the user clicks a segment, set it to `"claude"` or `"extra"`.
- Each SSE tick re-evaluates `default_mode`, but only uses it when `user_override` is `null`. Once overridden, the choice sticks for the session. No persistence.
- If the non-default side has no data (e.g. extra disabled): render that segment greyed, clicks are no-ops, and `user_override` can never land on it.

### Data contract

No backend changes. The flat JSON already exposes:

- `claude_usage_active`, `claude_usage_health`, `claude_usage_pct`, `claude_usage_window`, `claude_usage_model`, etc.
- `extra_usage_enabled`, `extra_usage_monthly_limit`, `extra_usage_used`, `extra_usage_pct`

The card reads whichever set matches the current mode.

## Layout

- Collapse the `summary-cards` grid and the `cards` grid into a single grid.
- `grid-template-columns: repeat(4, 1fr)` at wide widths.
- Reuse the existing breakpoints at ~1499 / ~1502 and ~1600 / ~1608 for `repeat(2, 1fr)` → `1fr` fallback.
- Remove the now-dead summary-grid CSS block.
- Order in the DOM: Combined, Usage, RTK, Headroom.

## Test plan

- `pytest tests/test_app.py` — must be green after the code purge, before any UI work.
- New assertions:
  - Flat contract has no key matching `^(jcodemunch|jdocmunch)_`.
  - Rendered dashboard HTML contains `#summary-usage` and does not contain `#summary-claude`, `#summary-extra`, `jcodemunch-card`, `jdocmunch-card`.
- Manual smoke test after rebuild:
  - Dashboard loads, four cards visible in one row.
  - Usage card defaults to Extra when extra is active, Claude otherwise.
  - Toggle flips content; disabled segment is unclickable.
  - Narrow window collapses to 2×2 then 1-column.
  - No 500s in server log; no console errors in browser.

## Execution order

1. Branch `chore/drop-jcode-jdoc` off `main`.
2. Uninstall binaries (reversible, independent).
3. Scrub `~/.claude/settings.json` permission and delete `CLAUDE.md.bak`.
4. Gut `app.py` collectors, state, contract keys, card HTML/JS.
5. Update `tests/test_app.py`; run `pytest` — must pass.
6. Rewrite summary/card HTML + CSS grid + Usage toggle JS.
7. `./build.sh`, smoke-test in browser.
8. Update `README.md`, `CHANGELOG.md`, `.claude/settings.local.json`.
9. Commit as two commits:
   - `chore: remove jcodemunch/jdocmunch collectors, cards, permissions`
   - `feat: merge claude/extra usage into toggleable card, align to single row`

## Risks

- **Binary uninstall detection** — if the install method isn't discoverable, we fall back to `rm`, which is still safe but bypasses package metadata.
- **Shell rc references** — a missed alias (`alias jcm=...` was seen in a snapshot) could leave dead references. Mitigation: grep all rc files before removing binaries.
- **Flat contract consumers** — anything reading `/statusline` JSON that still expects jcode/jdoc keys will break. Mitigation: grep the project for those keys before deleting; none are expected outside `app.py` + tests.
- **Toggle default drift** — if `extra_usage_enabled` flaps, the card could flip between defaults mid-session. Mitigation: the sticky `user_override` absorbs this once the user clicks; without a click, flapping is expected and correct.
