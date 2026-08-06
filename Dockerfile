FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the MCP server code
COPY retell-quo-server/ /app/

# Install Python deps
# CRITICAL pins (verified live 2026-08-07):
#   - mcp<2.0.0: v2.0.0 removed `mcp.server.fastmcp`
#   - starlette<0.38: mcp 1.9 brought starlette 1.4 which removed Router's `on_startup` kwarg
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "mcp>=1.9.0,<2.0.0" "starlette<0.38" httpx

# Sanity check
RUN python -c "import src.mcp_server, src.mcp_main; print('Modules loaded OK')"

# Run the MCP server via the FastAPI wrapper (with bearer auth + no trailing-slash redirect)
ENV PORT=8080
ENV BACKEND_URL=https://bk-jr-api.aixlabs.fun
EXPOSE 8080

CMD ["python", "-m", "src.mcp_main", "--host", "0.0.0.0", "--port", "8080"]
