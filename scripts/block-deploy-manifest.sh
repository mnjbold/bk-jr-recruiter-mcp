#!/usr/bin/env bash
# block-deploy-manifest.sh
# Pre-commit hook: refuse to commit render.yaml / fly.toml / docker-compose.prod.yml
# (these historically embedded plaintext API keys — use Coolify env vars instead)
set -e
for f in "$@"; do
  echo "FATAL: $f re-introduced a deployment manifest with embedded secrets."
  echo "       Use Coolify env vars (or another secret manager), not git."
done
exit 1