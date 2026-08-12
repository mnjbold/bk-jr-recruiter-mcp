#!/usr/bin/env python3
"""
scan_secrets.py — pre-commit hook to detect plaintext secrets in staged files.

Catches common API key / token formats even if gitleaks misses them:
  - Retell API keys (key_XXXXX...)
  - OpenAI / Stripe-style keys (sk-XXXXX...)
  - JWT / Supabase tokens (eyJhbGciOi...)
  - AWS access keys (AKIAXXXXX...)
  - GitHub OAuth tokens (gho_XXXXX...)
  - Slack bot tokens (xoxb-XXXXX...)
  - Hardcoded password/secret/token assignments
  - Bearer tokens in code

Exits 1 with a clear error report if any match. Exits 0 on clean.

Usage: python scan_secrets.py <file1> <file2> ...
"""
import re
import sys

PATTERNS: list[tuple[str, str]] = [
    (r"key_[a-f0-9]{20,}", "Retell API key"),
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI / Stripe-style key"),
    (r"eyJhbGciOi[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+", "JWT / Supabase token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"gho_[a-zA-Z0-9]{20,}", "GitHub OAuth token"),
    (r"xoxb-[a-zA-Z0-9-]{20,}", "Slack bot token"),
    (r'(?i)(password|secret|token)\s*=\s*["\'][^"\']{16,}["\']', "hardcoded password/secret/token"),
    (r"Bearer\s+[A-Za-z0-9_-]{20,}", "Bearer token in code"),
]

# Files we deliberately skip — pre-commit framework files themselves
# and known-safe test fixtures.
EXCLUDE_PATH_FRAGMENTS = (
    "/.pre-commit-config.yaml",
    "/scripts/scan_secrets.py",
    "/scripts/block-deploy-manifest.sh",
    "/scripts/block-env-files.sh",
    "/SECURITY-NOTES.md",  # documents historical exposures — values are redacted
    "/.git/",
    "/.venv/",
    "/node_modules/",
    "/__pycache__/",
)


def main(files: list[str]) -> int:
    matched: list[str] = []
    for fname in files:
        # Skip excluded paths
        if any(frag in fname for frag in EXCLUDE_PATH_FRAGMENTS):
            continue
        try:
            with open(fname, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except (OSError, IOError):
            continue
        for pat, label in PATTERNS:
            for m in re.finditer(pat, content):
                line_no = content[: m.start()].count("\n") + 1
                snippet = m.group()[:40] + ("..." if len(m.group()) > 40 else "")
                matched.append(f"  {fname}:{line_no}: {label} -- {snippet}")
    if matched:
        print("\nSECRET SCAN FAILED — the following patterns were detected:")
        for line in matched:
            print(line)
        print("\nACTION:")
        print("  1. Move the secret to Coolify env vars (or your secret manager).")
        print("  2. Replace the literal with an env-var read: os.environ['FOO']")
        print("  3. If this is a known false-positive, add the file/pattern to")
        print("     scripts/scan_secrets.py's EXCLUDE_PATH_FRAGMENTS.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))