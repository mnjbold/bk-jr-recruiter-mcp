FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the MCP server code (both backend + MCP live in this same tree)
COPY retell-quo-server/ /app/

# Belt-and-suspenders: remove any __pycache__ from the COPY (they may carry
# .pyc files from a different Python version, which break imports).
RUN find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Install Python deps
# CRITICAL pins (verified live 2026-08-07):
#   - mcp<2.0.0: v2.0.0 removed `mcp.server.fastmcp`
#   - starlette<0.38: mcp 1.9 brought starlette 1.4 which removed Router's `on_startup` kwarg
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "mcp>=1.9.0,<2.0.0" "starlette<0.38" httpx

# Use the MODE env var to choose which process to run.
# Render sets this per service in render.yaml. Both services share one image.
ENV MODE=backend
ENV PORT=8080
EXPOSE 8080

# Mode-switching entrypoint script — written as a separate file so the JSON
# array CMD can stay simple (avoids quoting hell in the array form).
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
