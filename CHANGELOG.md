# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-04-09

Activity feed realtime rewrite. The LIVE ACTIVITY feed now updates within
~2 seconds of the underlying tool running, holds up to 100 scrollable items,
and no longer lets bursty rtk activity push jcodemunch and jdocmunch entries
out of the viewable window.

### Added

- `DashboardCollector` background daemon thread that ticks every 250 ms and
  stores the latest payload as a shared snapshot under a lock. The `/events`
  SSE endpoint became a thin reader on a 2 s heartbeat, decoupling data
  freshness from transmission cadence.
- `_group_history()` pass wired into `collect_all()` with a 10 s burst window
  to collapse consecutive same-tool same-prefix entries so rtk bursts do not
  dominate the 100-slot feed.
- Thin dark scrollbar on the activity feed, matching the terminal aesthetic.
- Dynamic `showing last N` feed count label that reflects the actual
  rendered row count.
- `CLAUDE TOOLS` header is now a link to the project source on GitHub.
- Cold-start guard on the COMBINED banner so it reads `0` instead of
  `undefined` for the first ~2 s after service restart.
- Three new `_sanitise_cmd` regex patterns for HTTP auth headers
  (`Authorization: token/Bearer/Basic`), `X-*-Key` / `X-*-Token` custom
  headers, and `curl -u user:pass`.

### Changed

- `SSE_INTERVAL` default lowered from 30 s to 2 s. Now only a heartbeat,
  not a data-freshness bound.
- All history caps bumped from 20 to 100: RTK SQLite query limit,
  headroom / jcodemunch / jdocmunch in-memory rings, `collect_all` merged
  trim, and the frontend render loop.
- `collect_rtk`, `collect_jcodemunch`, `collect_jdocmunch` now read version
  strings from a cache populated once at collector startup instead of
  spawning version subprocesses on every SSE poll.
- `collect_jcodemunch` now watches the max mtime across `session_stats.json`,
  `_savings.json`, and all `local-*.db` files (previously only
  `session_stats.json`), catching more jcodemunch activity between the
  upstream tool's periodic flushes.

### Fixed

- Pre-existing security gap: `_sanitise_cmd` only redacted `KEY=value` style
  env vars and Anthropic `sk-ant-*` keys. HTTP auth headers from rtk-wrapped
  curl commands were passed through unredacted. The 20 to 100 cap bump made
  this more visible because older commands stayed in the viewable feed for
  longer. Patterns extended to cover the common auth header shapes. The
  historical rtk SQLite database on the running host was also scrubbed of
  leaked values.

## [1.1.0] - 2026-03-31

Dashboard improvement pass: feed grouping, progress bars, health indicators,
visual polish.

### Added

- Tool health indicator dots in card headers (ok / stale / error).
- Progress bars repurposed: RTK efficiency, headroom compression ratio,
  jcodemunch and jdocmunch freshness (decays linearly over 60 minutes of
  inactivity).
- Smart activity feed grouping in `collect_all` to collapse bursty command
  sequences into single grouped entries (see v1.2.0 for the final wiring).

### Changed

- Feed noise reduction and updated styling for grouped entries.

### Fixed

- Ticker readability, unicode clock, and desktop overflow issues.
- Corrected the RTK repository link to `rtk-ai/rtk`.

## [1.0.0] - 2026-03-31

Initial public release.

### Added

- Single-file Flask dashboard (`app.py`) with embedded HTML / CSS / JS.
- SSE-backed live update of token savings for rtk, headroom, jcodemunch,
  and jdocmunch, with per-tool cards, sparklines, and a live activity feed.
- Weekly savings tracker with current week, last week, daily burn rate,
  and reset countdown derived from the Anthropic usage API.
- `/health` endpoint for service monitoring.

### Fixed

- Mobile viewport scrolling.

[1.2.0]: https://github.com/Will-Luck/claude-tools-dashboard/releases/tag/v1.2.0
[1.1.0]: https://github.com/Will-Luck/claude-tools-dashboard/releases/tag/v1.1.0
[1.0.0]: https://github.com/Will-Luck/claude-tools-dashboard/releases/tag/v1.0.0
