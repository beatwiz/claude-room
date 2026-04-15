#!/usr/bin/env bash
# build.sh -- rebuild and (re)launch the claude-tools-dashboard container.
#
# Replaces the old `cct` shell function. No more ~/.claude credentials
# mount: Claude subscription values now come from Headroom's /stats
# subscription_window endpoint, so the container doesn't need any Anthropic
# credentials to show usage percentages.
#
# Overridable via env vars (defaults shown):
#   IMAGE            claude-tools-dashboard
#   CONTAINER        claude-tools-dashboard
#   DOCKER_NETWORK   cowork-net
#   PORT             8095
#   HEADROOM_URL     http://host.docker.internal:8787
#   TZ               Europe/Lisbon

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IMAGE:-claude-tools-dashboard}"
CONTAINER="${CONTAINER:-claude-tools-dashboard}"
NETWORK="${DOCKER_NETWORK:-cowork-net}"
PORT="${PORT:-8095}"
HEADROOM_URL="${HEADROOM_URL:-http://host.docker.internal:8787}"
TZ_VALUE="${TZ:-Europe/Lisbon}"

# Optional: capture host-installed tool versions so the dashboard can display
# them without bundling the binaries. Empty string -> dashboard renders
# "unknown" for that tool.
rtk_v="$(rtk --version 2>/dev/null | awk '{print $NF}' || true)"
jcm_v="$(jcodemunch-mcp --version 2>/dev/null | awk '{print $NF}' || true)"
jdm_v="$(pip show jdocmunch-mcp 2>/dev/null | awk '/^Version:/ {print $2}' || true)"

echo "build.sh: building image '$IMAGE' from $SCRIPT_DIR"
docker build -t "$IMAGE" "$SCRIPT_DIR" >/dev/null

echo "build.sh: replacing container '$CONTAINER'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# macOS-specific path for rtk's SQLite DB lives under Application Support.
# The code-index / doc-index / cache dirs follow the default locations used
# by jcodemunch-mcp, jdocmunch-mcp, and this app respectively.
docker run -d \
    --name "$CONTAINER" \
    --network "$NETWORK" \
    --restart unless-stopped \
    -p "$PORT:$PORT" \
    -e PORT="$PORT" \
    -e HEADROOM_URL="$HEADROOM_URL" \
    -e TZ="$TZ_VALUE" \
    -e RTK_VERSION="$rtk_v" \
    -e JCODEMUNCH_VERSION="$jcm_v" \
    -e JDOCMUNCH_VERSION="$jdm_v" \
    -v "$HOME/.code-index:/root/.code-index" \
    -v "$HOME/.doc-index:/root/.doc-index" \
    -v "$HOME/Library/Application Support/rtk:/root/.local/share/rtk" \
    -v "$HOME/.cache/claude-tools-dashboard:/root/.cache/claude-tools-dashboard" \
    "$IMAGE" >/dev/null

echo "build.sh: http://127.0.0.1:$PORT"
