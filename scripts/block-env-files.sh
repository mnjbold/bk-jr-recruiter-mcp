@echo off
REM block-env-files.sh
REM Pre-commit hook: refuse to commit .env / .env.local / .env.production
for %%f in %* do (
  echo FATAL: %%f contains env vars. Use Coolify env vars or a secret manager, not git.
)
exit /b 1