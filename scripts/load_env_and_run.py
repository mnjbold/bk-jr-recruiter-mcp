#!/usr/bin/env python3
"""
load_env_and_run.py — Load .env.local then exec the given command.

Replaces the unreliable .bat `for /f delims==` loader. Handles:
  - KEY=VALUE lines (with optional quotes)
  - # comments and blank lines
  - 'export' prefix (bash-style)
  - Line continuation (backslash-newline, ignored for simplicity)

Usage (from hermes manifest or shell):
    python scripts/load_env_and_run.py -- uvicorn src.server:app --port 18080
    python scripts/load_env_and_run.py --mode mcp -- python -m src.mcp_main --port 18080

Security: .env.local MUST be gitignored. This script never logs values.
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file. Returns dict of env vars to set."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip optional `export ` prefix
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if not key:
            continue
        out[key] = val
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", type=Path, default=ENV_FILE,
                    help="Path to .env file (default: .env.local at repo root)")
    ap.add_argument("--mode", choices=["backend", "mcp"], default="backend",
                    help="Which service to start (sets MODE env var)")
    ap.add_argument("--port", type=int, default=18080, help="Port (default 18080)")
    ap.add_argument("--print-only", action="store_true",
                    help="Just print what would be executed, don't run it")
    ap.add_argument("cmd", nargs=argparse.REMAINDER,
                    help='Command to exec after `--`. e.g. -- uvicorn ...')
    args = ap.parse_args()

    # Strip the leading `--` if present
    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]

    env = os.environ.copy()
    env.update(load_env_file(args.env_file))
    env["MODE"] = args.mode
    env["PORT"] = str(args.port)
    env["PYTHONPATH"] = str(ROOT / "retell-quo-server")

    # Tell the user what was loaded (without leaking values)
    loaded = load_env_file(args.env_file)
    keys_summary = ", ".join(sorted(loaded.keys())) or "(none)"
    print(f"[load_env] {len(loaded)} env vars loaded from {args.env_file}: {keys_summary}",
          file=sys.stderr)
    print(f"[load_env] MODE={args.mode} PORT={args.port} PYTHONPATH={env['PYTHONPATH']}",
          file=sys.stderr)

    if args.print_only or not args.cmd:
        print(f"[load_env] would exec: {' '.join(shlex.quote(c) for c in args.cmd)}",
              file=sys.stderr)
        return 0

    # Replace this process with the target command
    try:
        return subprocess.call(args.cmd, env=env, cwd=str(ROOT / "retell-quo-server"))
    except FileNotFoundError as e:
        print(f"[load_env] command not found: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())