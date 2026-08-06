#!/bin/sh
# Mode-switching entrypoint for the BK Jr. Render deploy.
# MODE=backend (default) -> uvicorn src.server:app
# MODE=mcp                -> python -m src.mcp_main (the auth-wrapped MCP dispatcher)
# Render sets MODE in render.yaml per service.

set -e

if [ "$MODE" = "mcp" ]; then
    echo "[entrypoint] MODE=mcp — starting MCP server on port ${PORT:-8080}"
    exec python -m src.mcp_main --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips="*"
else
    echo "[entrypoint] MODE=backend — starting uvicorn src.server:app on port ${PORT:-8080}"
    exec uvicorn src.server:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips="*"
fi
