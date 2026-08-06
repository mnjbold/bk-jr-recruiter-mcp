"""
Pure ASGI wrapper around the BK Jr. MCP server — with bearer auth AND
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

# IMPORTANT: set stateless_http BEFORE importing the streamable app
# (some FastMCP internals read this at import-time).
from .mcp_server import mcp

# Fix #3: avoid the "Task group is not initialized" error by running in
# stateless mode. Each MCP request is then independent.
mcp.settings.stateless_http = True


def health_app(scope, receive, send):
    """Minimal ASGI app for the /health endpoint. No auth required."""
    if scope["type"] != "http":
        return
    if scope["path"] != "/health":
        send({"type": "http.response.start", "status": 404, "headers": []})
        send({"type": "http.response.body", "body": b""})
        return
    body = json.dumps({
        "status": "ok",
        "server": "bk-jr-recruiting",
        "version": "1.9.0",
        "tools": 24,
        "auth": "bearer-required",
    }).encode()
    send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    send({"type": "http.response.body", "body": body})


def _extract_bearer(headers):
    """Pull the bearer token from the Authorization header. Returns None if missing/malformed."""
    for name, value in headers:
        if name == b"authorization":
            try:
                v = value.decode("latin-1") if isinstance(value, bytes) else value
            except Exception:
                return None
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return None


def _expected_token():
    """The expected bearer token. Falls back to SMS_AGENT_API_KEY for compat."""
    return os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("SMS_AGENT_API_KEY") or ""


def _send_401(send, msg):
    body = json.dumps({"error": "unauthorized", "message": msg}).encode()
    send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b'Bearer realm="bk-jr-recruiter"'),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    send({"type": "http.response.body", "body": body})


def _send_404(send):
    body = b""
    send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-length", b"0")],
    })
    send({"type": "http.response.body", "body": body})


async def dispatcher(scope, receive, send):
    """Pure ASGI dispatcher with bearer auth.

    Routes:
      - /health            -> no auth, returns OK
      - /mcp, /mcp/        -> requires bearer auth, then MCP server
      - /mcp/xxx           -> requires bearer auth, then MCP server
      - everything else    -> 404
    """
    if scope["type"] != "http":
        return

    path = scope["path"]

    # Health check (no auth — Render needs to reach it without credentials)
    if path == "/health":
        await health_app(scope, receive, send)
        return

    # MCP server paths
    if path == "/mcp" or path.startswith("/mcp/"):
        # === AUTH CHECK ===
        token = _extract_bearer(scope.get("headers") or [])
        expected = _expected_token()
        if not expected:
            _send_401(send, "server has no auth token configured; set MCP_AUTH_TOKEN")
            return
        if not token or token != expected:
            _send_401(send, "invalid or missing bearer token")
            return

        # Auth passed — route to the MCP server. Use /mcp as the canonical path
        # (strip trailing slash from /mcp/ so the MCP app's internal routing
        # at /mcp catches it without a 307).
        if path == "/mcp/":
            new_path = "/mcp"
        else:
            new_path = path
        new_scope = dict(scope)
        new_scope["path"] = new_path
        new_scope["raw_path"] = new_path.encode()
        await mcp.streamable_http_app()(new_scope, receive, send)
        return

    # 404
    _send_404(send)


def main() -> None:
    parser = argparse.ArgumentParser(description="BK Jr. MCP server (pure ASGI, with bearer auth)")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    if not _expected_token():
        print("WARNING: MCP_AUTH_TOKEN (or SMS_AGENT_API_KEY) not set — server will reject ALL MCP requests with 401.")
        print("         Set MCP_AUTH_TOKEN env var before starting.")

    uvicorn.run(
        dispatcher,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
