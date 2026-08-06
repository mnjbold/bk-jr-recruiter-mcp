#!/bin/sh
# Mode-switching entrypoint for the BK Jr. Render deploy.
# MODE=backend (default) -> uvicorn src.server:app
# MODE=mcp                -> python -m src.mcp_main (the auth-wrapped MCP dispatcher)
# Render sets MODE in render.yaml per service.
#
# IMPORTANT: do NOT pass --proxy-headers / --forwarded-allow-ips to
# `python -m src.mcp_main` — mcp_main.py's argparse only knows about
# --host and --port. The mcp_main module hardcodes proxy_headers=True and
# forwarded_allow_ips="*" in its own uvicorn.run() call. Passing them
# to the mcp_main CLI causes argparse to fail with exit code 2.

set -e

if [ "$MODE" = "mcp" ]; then
    echo "[entrypoint] MODE=mcp — starting MCP server on port ${PORT:-8080}"
    exec python -m src.mcp_main --host 0.0.0.0 --port "${PORT:-8080}"
else
    echo "[entrypoint] MODE=backend — starting uvicorn src.server:app on port ${PORT:-8080}"
    exec uvicorn src.server:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips="*"
fi
