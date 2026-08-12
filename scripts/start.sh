#!/usr/bin/env bash
# start.sh — launch BK JR MCP backend for local dev/hermes verification
#
# Sources .env.local (gitignored, real keys), then starts uvicorn.
# Use this from hermes verify or any local launcher.
#
# Usage:
#   bash scripts/start.sh           # backend (MODE=backend) on :18080
#   bash scripts/start.sh mcp       # MCP server (MODE=mcp) on :18080
#   PORT=9000 bash scripts/start.sh  # override port
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT/retell-quo-server"

# Load env from .env.local if present (the only place real keys live in dev)
if [ -f "$ROOT/.env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.local"
  set +a
fi

MODE="${1:-backend}"
export MODE
export PORT="${PORT:-18080}"
export PYTHONPATH="${PYTHONPATH:-.}"

# Strip any leftover dummy values the agent.py tolerates but QuoClient doesn't
[ "$QUO_API_KEY" = "dummy" ] && unset QUO_API_KEY
[ "$RETELL_API_KEY" = "dummy" ] && unset RETELL_API_KEY

echo "[start.sh] MODE=$MODE PORT=$PORT"
echo "[start.sh] QUO_API_KEY=${QUO_API_KEY:+set(${QUO_API_KEY:0:4}***)}"
echo "[start.sh] RETELL_API_KEY=${RETELL_API_KEY:+set(${RETELL_API_KEY:0:4}***)}"
echo "[start.sh] OPENPHONE_API_KEY=${OPENPHONE_API_KEY:+set(${OPENPHONE_API_KEY:0:4}***)}"
echo "[start.sh] COMPOSIO_API_KEY=${COMPOSIO_API_KEY:+set}"

if [ "$MODE" = "mcp" ]; then
  exec python -m src.mcp_main --host 0.0.0.0 --port "$PORT"
else
  exec uvicorn src.server:app --host 0.0.0.0 --port "$PORT" \
    --proxy-headers --forwarded-allow-ips="*"
fi