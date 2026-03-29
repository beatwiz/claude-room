# Claude Tools Dashboard

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ed?logo=docker&logoColor=white)](Dockerfile)

Live wallboard for monitoring token savings across your Claude Code toolchain. Tracks [RTK](https://github.com/reachingforthejack/rtk), [Headroom](https://github.com/chopratejas/headroom), and [jCodeMunch](https://github.com/jgravelle/jcodemunch-mcp) in a single-page dashboard with real-time SSE updates.

![Dashboard](screenshots/dashboard-full.png)

## What it shows

- **RTK** -- command-level token savings from the CLI proxy (SQLite)
- **Headroom** -- context compression stats from the MCP server (HTTP API)
- **jCodeMunch** -- indexed repos and session savings (filesystem + MCP)
- **Combined total** with sparkline trends and live activity feed

## Quick start

```bash
# Clone and run locally
git clone <repo-url>
cd claude-tools-dashboard
pip install -r requirements.txt
python app.py
# Open http://localhost:8095
```

### Docker

```bash
docker build -t claude-tools-dashboard .
docker run -d --name claude-tools-dashboard \
  -p 8095:8095 \
  -v ~/.local/share/rtk:/root/.local/share/rtk:ro \
  -v ~/.code-index:/root/.code-index:ro \
  --network host \
  claude-tools-dashboard
```

Use `--network host` so the container can reach the Headroom proxy on localhost. Alternatively, set `HEADROOM_URL` to point at the host IP.

## Configuration

All settings via environment variables. Copy `.env.example` for reference:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8095` | Dashboard listen port |
| `HEADROOM_URL` | `http://127.0.0.1:8787` | Headroom proxy stats endpoint |
| `RTK_DB_PATH` | `~/.local/share/rtk/history.db` | RTK SQLite database |
| `RTK_BIN` | `rtk` | Path to RTK binary |
| `JCODEMUNCH_INDEX_DIR` | `~/.code-index` | jCodeMunch index directory |
| `JCODEMUNCH_BIN` | `jcodemunch-mcp` | Path to jCodeMunch binary |
| `SSE_INTERVAL` | `30` | Seconds between SSE pushes |

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

## License

MIT
