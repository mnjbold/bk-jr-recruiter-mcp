"""
Starlette + FastMCP wrapper for BK Jr. with bearer auth.

Three problems solved here:

1. **Trailing-slash redirect**: Forward /mcp and /mcp/ directly to the FastMCP
   session manager (bypassing the internal Mount that 307-redirects).

2. **No built-in auth**: When exposed publicly, anyone with the URL can call
   all 24 tools — including placing real Retell calls and sending real Quo SMS.
   This wrapper REQUIRES a Bearer token (MCP_AUTH_TOKEN or SMS_AGENT_API_KEY).

3. **Task group is not initialized**: FastMCP 1.9.0's session_manager
   requires a task group, even in stateless mode (a library bug). We use
   a Starlette lifespan that wraps session_manager.run() to create the
   task group. We also monkey-patch handle_request to skip the
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
# create the real task group, but if for some reason it's None, we provide
# a no-op so the handler doesn't crash).
import mcp.server.streamable_http_manager as _shm

_orig_handle_request = _shm.StreamableHTTPSessionManager.handle_request


async def _patched_handle_request(self, scope, receive, send):
    """Skip the task group check entirely — we have a real task group from
    the lifespan, and stateless mode doesn't actually need it."""
    if self.stateless:
        await self._handle_stateless_request(scope, receive, send)
        return
    await _orig_handle_request(self, scope, receive, send)


_shm.StreamableHTTPSessionManager.handle_request = _patched_handle_request


def _expected_token() -> str:
    """The expected bearer token. Falls back to SMS_AGENT_API_KEY for compat."""
    return os.environ.get("MCP_AUTH_TOKEN") or os.environ.get("SMS_AGENT_API_KEY") or ""


def _extract_bearer(headers):
    """Pull the bearer token from the ASGI headers list. Returns None if missing/malformed."""
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


# Get the FastMCP session manager (we bypass the Mount at /mcp that does
# the 307-redirect). The session manager is created lazily by streamable_http_app().
# Call it once to force creation, then grab the manager.
_mcp_app = mcp.streamable_http_app()  # forces session_manager creation
mcp_session_manager = mcp._session_manager


async def dispatcher(scope, receive, send):
    """Pure ASGI dispatcher with bearer auth + lifespan handling."""
    # Handle lifespan events so the session_manager.run() context can be entered
    if scope["type"] == "lifespan":
        # Enter session_manager.run() context, which creates the task group
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
        # unreachable

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

        # Auth passed — call the session manager's _handle_stateless_request
        # directly. This bypasses the FastMCP app's internal Mount (which does
        # a 307 redirect) and the handle_request task_group check (monkey-patched).
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
