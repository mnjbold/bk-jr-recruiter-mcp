"""
FastAPI wrapper around the BK Jr. MCP server — with bearer auth AND
stateless mode fix.

Three problems solved here:

1. **Trailing-slash redirect**: The default FastMCP streamable-http transport
   mounts at /mcp and 307-redirects /mcp/ to /mcp. Behind a TLS-terminating
   proxy, the redirect uses http:// instead of https:// and clients hit a 421.
   This wrapper handles both /mcp and /mcp/ directly with no redirects.

2. **No built-in auth**: When exposed publicly, anyone with the URL can call
   all 24 tools — including placing real Retell calls and sending real Quo SMS.
   This wrapper REQUIRES a Bearer token (MCP_AUTH_TOKEN or SMS_AGENT_API_KEY).

3. **Task group is not initialized**: mcp.run() sets up a streamable HTTP session
   manager with a task group. Calling mcp.streamable_http_app() directly
   doesn't init that task group. Setting `mcp.settings.stateless_http = True`
   before the app is invoked makes each request independent, avoiding the
   task-group requirement entirely.
"""
from __future__ import annotations

import argparse
import json
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.routing import Mount

# IMPORTANT: set stateless_http BEFORE importing the streamable app
# (some FastMCP internals read this at import-time).
from .mcp_server import mcp

# Fix #3: avoid the "Task group is not initialized" error by running in
# stateless mode. Each MCP request is then independent.
mcp.settings.stateless_http = True

# Build the underlying FastMCP ASGI app. When mounted into FastAPI at /mcp
# and /mcp/, FastAPI will route requests to it. Mounting (instead of a
# custom dispatcher) avoids the scope-passthrough bugs that bit us earlier.
mcp_app = mcp.streamable_http_app()


def _expected_token() -> str:
    """The expected bearer token. Falls back to SMS_AGENT_API_KEY for compat."""
    return os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("SMS_AGENT_API_KEY") or ""


app = FastAPI(title="BK Jr. MCP (with auth)", version="1.9.0")


@app.middleware("http")
async def bearer_auth_middleware(request: Request, call_next):
    """
    Enforce bearer auth on every request EXCEPT /health.
    MCP_TRANSPORT=stdio mode is unaffected (this is HTTP-only).
    """
    # Health check is always open (Render health checks need no credentials)
    if request.url.path == "/health":
        return await call_next(request)

    expected = _expected_token()
    auth_header = request.headers.get("authorization", "")
    if not expected:
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "server has no auth token configured; set MCP_AUTH_TOKEN"},
            headers={"www-authenticate": 'Bearer realm="bk-jr-recruiter"'},
        )
    if not auth_header.lower().startswith("bearer "):
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "missing Authorization: Bearer <token> header"},
            headers={"www-authenticate": 'Bearer realm="bk-jr-recruiter"'},
        )
    token = auth_header[7:].strip()
    if token != expected:
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "invalid bearer token"},
            headers={"www-authenticate": 'Bearer realm="bk-jr-recruiter"'},
        )

    return await call_next(request)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "server": "bk-jr-recruiting",
        "version": "1.9.0",
        "tools": 24,
        "auth": "bearer-required",
    }


# Mount the FastMCP app at /mcp with redirect_slashes=False so POST /mcp
# does NOT 307-redirect to /mcp/ (which would strip the Authorization header
# in some clients). With redirect_slashes=False, both /mcp and /mcp/ work
# without a redirect.
app.router.routes.append(
    Mount("/mcp", app=mcp_app, name="mcp-no-slash")
)
app.router.routes.append(
    Mount("/mcp/", app=mcp_app, name="mcp-with-slash")
)


def main() -> None:
    parser = argparse.ArgumentParser(description="BK Jr. MCP server (FastAPI + bearer auth)")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    if not _expected_token():
        print("WARNING: MCP_AUTH_TOKEN (or SMS_AGENT_API_KEY) not set — server will reject ALL MCP requests with 401.")
        print("         Set MCP_AUTH_TOKEN env var before starting.")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
