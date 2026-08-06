"""
Pure ASGI wrapper around the BK Jr. MCP server — with bearer auth.

The FastMCP streamable HTTP transport has a Mount at /mcp that does a 307 redirect,
which breaks the request on Render (the redirect lands on an "Invalid Host header"
421). We bypass the Mount and call the session manager directly.

Two problems solved here:

1. **No built-in auth**: When exposed publicly, anyone with the URL can call
   all 24 tools — including placing real Retell calls and sending real Quo SMS.
   This wrapper REQUIRES a Bearer token (MCP_AUTH_TOKEN or SMS_AGENT_API_KEY).

2. **Task group is not initialized**: FastMCP 1.9.0's session_manager
   requires a task group, even in stateless mode (a library bug). We use
   a Starlette-style lifespan that wraps session_manager.run() to create
   the task group. We also monkey-patch handle_request to skip the
   unconditional task group check (defense in depth).
"""
from __future__ import annotations

import argparse
import json
import os

import uvicorn

# IMPORTANT: set stateless_http BEFORE importing the streamable app
from .mcp_server import mcp

# Stateless mode: each request is independent.
mcp.settings.stateless_http = True

# WORKAROUND for FastMCP 1.9.0 bug: the session_manager.handle_request
# unconditionally checks `if self._task_group is None: raise`. We patch it
# to skip the check (we still call session_manager.run() via lifespan to
# create the real task group).
import mcp.server.streamable_http_manager as _shm

_orig_handle_request = _shm.StreamableHTTPSessionManager.handle_request


async def _patched_handle_request(self, scope, receive, send):
    """Skip the task group check — we have a real task group from the lifespan."""
    if self.stateless:
        await self._handle_stateless_request(scope, receive, send)
        return
    await _orig_handle_request(self, scope, receive, send)


_shm.StreamableHTTPSessionManager.handle_request = _patched_handle_request


# Force session_manager creation (lazy via streamable_http_app).
_mcp_app = mcp.streamable_http_app()
mcp_session_manager = mcp._session_manager


def _expected_token() -> str:
    return os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("SMS_AGENT_API_KEY") or ""


def _extract_bearer(headers):
    for name, value in headers:
        if name == b"authorization":
            try:
                v = value.decode("latin-1") if isinstance(value, bytes) else value
            except Exception:
                return None
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return None


async def _send_401(send, msg):
    body = json.dumps({"error": "unauthorized", "message": msg}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b'Bearer realm="bk-jr-recruiter"'),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_json(send, status, body_dict):
    body = json.dumps(body_dict).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def health_app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["path"] != "/health":
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return
    body = json.dumps({
        "status": "ok",
        "server": "bk-jr-recruiting",
        "version": "1.9.0",
        "tools": 24,
        "auth": "bearer-required",
    }).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def dispatcher(scope, receive, send):
    """Pure ASGI dispatcher with bearer auth + lifespan handling."""
    # Handle lifespan: enter session_manager.run() context to create the task group
    if scope["type"] == "lifespan":
        cm = mcp_session_manager.run()
        await cm.__aenter__()
        try:
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        finally:
            await cm.__aexit__(None, None, None)
        return

    if scope["type"] != "http":
        return

    path = scope["path"]

    # Health check (no auth)
    if path == "/health":
        await health_app(scope, receive, send)
        return

    # MCP server paths — require auth, then call the session manager directly
    if path == "/mcp" or path.startswith("/mcp/"):
        token = _extract_bearer(scope.get("headers") or [])
        expected = _expected_token()
        if not expected:
            await _send_401(send, "server has no auth token configured; set MCP_AUTH_TOKEN")
            return
        if not token or token != expected:
            await _send_401(send, "invalid or missing bearer token")
            return

        # Auth passed — call the session manager directly.
        # The session_manager._handle_stateless_request expects the path to be
        # at the FastMCP's streamable_http_path (default "/mcp"). We pass the
        # original scope (path) — the session manager will route based on it.
        await mcp_session_manager._handle_stateless_request(scope, receive, send)
        return

    # 404
    await _send_json(send, 404, {"error": "not_found", "path": path})


def main() -> None:
    parser = argparse.ArgumentParser(description="BK Jr. MCP server (pure ASGI + bearer auth)")
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
