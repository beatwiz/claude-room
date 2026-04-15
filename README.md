# Claude Tools Dashboard

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docker Hub](https://img.shields.io/docker/v/willluck/claude-tools-dashboard?logo=docker&logoColor=white&label=Docker%20Hub)](https://hub.docker.com/r/willluck/claude-tools-dashboard)
[![GHCR](https://img.shields.io/badge/ghcr.io-available-2496ed?logo=github&logoColor=white)](https://ghcr.io/will-luck/claude-tools-dashboard)

Live wallboard for monitoring token savings across your Claude Code toolchain. Tracks [RTK](https://github.com/reachingforthejack/rtk), [Headroom](https://github.com/chopratejas/headroom), [jCodeMunch](https://github.com/jgravelle/jcodemunch-mcp), and [jDocMunch](https://github.com/jgravelle/jdocmunch-mcp) in a single-page dashboard with real-time SSE updates.

![Dashboard](screenshots/dashboard-full.png)

## What it shows

- **RTK** -- command-level token savings from the CLI proxy (SQLite)
- **Headroom** -- context compression stats from the MCP server (HTTP API)
- **jCodeMunch** -- indexed repos and session savings (filesystem + MCP)
- **jDocMunch** -- documentation indexing and section retrieval savings (filesystem)
- **Combined total** with sparkline trends and live activity feed
- **Stats ticker** -- weekly savings breakdown, daily burn rate, Claude usage percentages (5-hour, weekly, Sonnet), and reset countdown (sourced from Headroom's `subscription_window` stats -- no Claude credentials needed)

## Quick start

```bash
# Clone and run locally
git clone https://github.com/Will-Luck/claude-tools-dashboard.git
cd claude-tools-dashboard
pip install -r requirements.txt
python app.py
# Open http://localhost:8095
```

### Docker (pre-built)

```bash
# Docker Hub
docker run -d --name claude-tools-dashboard \
  -p 8095:8095 \
  -v ~/.local/share/rtk:/root/.local/share/rtk:ro \
  -v ~/.code-index:/root/.code-index:ro \
  -v ~/.doc-index:/root/.doc-index:ro \
  --network host \
  willluck/claude-tools-dashboard

# Or from GHCR
docker run -d ... ghcr.io/will-luck/claude-tools-dashboard
```

### Docker (build from source)

```bash
docker build -t claude-tools-dashboard .
docker run -d --name claude-tools-dashboard \
  -p 8095:8095 \
  -v ~/.local/share/rtk:/root/.local/share/rtk:ro \
  -v ~/.code-index:/root/.code-index:ro \
  -v ~/.doc-index:/root/.doc-index:ro \
  --network host \
  claude-tools-dashboard
```

Use `--network host` so the container can reach the Headroom proxy on localhost. Alternatively, set `HEADROOM_URL` to point at the host IP. Claude usage data (5-hour / weekly / Sonnet percentages and reset countdown) is pulled from Headroom's `/stats` endpoint -- no Claude credentials mount required.

## Configuration

All settings via environment variables. Copy `.env.example` for reference:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8095` | Dashboard listen port |
| `HEADROOM_URL` | `http://127.0.0.1:8787` | Headroom proxy stats endpoint |
| `RTK_DB_PATH` | `~/.local/share/rtk/history.db` | RTK SQLite database |
| `RTK_BIN` | `rtk` | Path to RTK binary |
| `JCODEMUNCH_INDEX_DIR` | `~/.code-index` | jCodeMunch index directory |
| `JDOCMUNCH_INDEX_DIR` | `~/.doc-index` | jDocMunch index directory |
| `JCODEMUNCH_BIN` | `jcodemunch-mcp` | Path to jCodeMunch binary |
| `SSE_INTERVAL` | `30` | Seconds between SSE pushes |
| `WEEKLY_CACHE_DIR` | `~/.cache/claude-tools-dashboard` | Weekly savings snapshot directory |

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard HTML (self-contained SPA) |
| `GET /health` | JSON health check |
| `GET /events` | SSE stream (auto-reconnects) |

## Architecture

Single-file Flask app (`app.py`) that:

1. Polls RTK's SQLite database for command history and savings
2. Queries Headroom's HTTP stats API for compression data
3. Reads jCodeMunch index files for repo and session metrics
4. Pushes aggregated state to connected browsers via SSE
5. Serves a self-contained HTML/CSS/JS dashboard (no build step)

The frontend uses vanilla JS with CSS custom properties for theming. Sparkline charts are drawn with inline SVG. No external dependencies beyond Flask.

## Tests

Install dev dependencies and run pytest from the repo root:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

The test suite covers the `_flatten_snapshot` helper contract and the
`GET /api/status` route. It does not exercise the individual
`collect_*` functions or the background collector thread.

## License

MIT
