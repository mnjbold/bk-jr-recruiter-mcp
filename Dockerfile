FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the MCP server code (both backend + MCP live in this same tree)
COPY retell-quo-server/ /app/

# Install Python deps
# CRITICAL pins (verified live 2026-08-07):
#   - mcp<2.0.0: v2.0.0 removed `mcp.server.fastmcp`
#   - starlette<0.38: mcp 1.9 brought starlette 1.4 which removed Router's `on_startup` kwarg
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "mcp>=1.9.0,<2.0.0" "starlette<0.38" httpx

# Sanity check
RUN python -c "import src.mcp_server, src.mcp_main, src.server; print('Modules loaded OK')"

# Use the MODE env var to choose which process to run.
# - MODE=backend (default) -> uvicorn src.server:app
# - MODE=mcp                -> python -m src.mcp_main (the auth-wrapped MCP dispatcher)
# Render sets this per service in render.yaml.
ENV MODE=backend
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "if [ \"$MODE\" = \"mcp\" ]; then python -m src.mcp_main --host 0.0.0.0 --port ${PORT:-8080}; else exec uvicorn src.server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'; fi"]
