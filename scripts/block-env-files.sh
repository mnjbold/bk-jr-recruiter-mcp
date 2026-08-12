#!/usr/bin/env bash
# block-env-files.sh
# Pre-commit hook: refuse to commit .env / .env.local / .env.production
# (.gitignore also blocks these, but this is a louder, clearer failure.)
set -e
for f in "$@"; do
  echo "FATAL: $f contains env vars. Use Coolify env vars or a secret manager, not git."
done
exit 1