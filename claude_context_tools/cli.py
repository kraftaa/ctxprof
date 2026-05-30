"""`claude-ctx` umbrella command dispatching to the dashboard, audit, install."""

from __future__ import annotations

import sys

from . import __version__, audit, dashboard, install

USAGE = """ctx — Claude Code context & cache tools

Usage:
  ctx dashboard [--refresh N] [--include-stale]   live multi-session monitor
  ctx show [SESSION]                              one session + audit command
  ctx steps [--limit N] [--json]                  recent per-turn cost feed (all sessions)
  ctx digest [--since 2h] [--json]                rollup: cost, cache writes, gaps
  ctx tui [--limit N]                             interactive step browser
  ctx audit [--latest|--session ID|--transcript P] [--json]
                                                  offline "why did it burn?" analyzer
  ctx statusline                                  heartbeat statusline (reads stdin)
  ctx install [--statusline] [--force]            wire the heartbeat statusline
  ctx version

Run any subcommand with --help for its options.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd in ("dashboard", "show", "statusline", "steps", "digest", "tui"):
        return dashboard.main([cmd, *rest])
    if cmd == "audit":
        return audit.main(rest)
    if cmd == "install":
        return install.main(rest)
    if cmd in ("version", "--version", "-V"):
        print(f"ctx (claude-context-tools) {__version__}")
        return 0

    print(f"unknown command: {cmd}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
