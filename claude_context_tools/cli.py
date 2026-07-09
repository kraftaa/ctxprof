"""`claude-ctx` umbrella command dispatching to the dashboard, audit, install."""

from __future__ import annotations

import sys

from . import __version__, audit, dashboard, guard, install, mcp_risk

USAGE = """ctx — Claude Code context & cache tools

Usage:
  ctx dashboard [--refresh N] [--include-stale]   live multi-session monitor
  ctx sessions [--include-stale] [--json]         list sessions for picking an id
  ctx show [SESSION]                              one session + audit command
  ctx watch [SESSION]                             live single-session panel + advice
  ctx explain [SESSION]                           root-cause why a session got expensive
  ctx steps [--limit N] [--json]                  recent per-turn cost feed (all sessions)
  ctx digest [--since 2h] [--json]                rollup: cost, cache writes, gaps
  ctx compare [--since 30d] [--deep] [--json]     cross-session cache reuse, rebuild waste, spend
  ctx rates [--context N] [--model M]             price table + keep-warm vs rebuild math
  ctx tui [--limit N]                             interactive step browser
  ctx audit [--latest|--session ID|--transcript P] [--json]
                                                  offline "why did it burn?" analyzer
  ctx guard [--latest|--session ID|--transcript P] [--json] [--strict] [--mcp]
                                                  scan a session for secrets, dangerous commands, taint, injection (--mcp: MCP risk)
  ctx attack-path [SESSION] [--json]              potential injection→action→exfil reachability for a session
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
    if cmd in ("dashboard", "sessions", "show", "statusline", "steps", "digest", "compare", "tui", "watch", "hook", "rates", "explain"):
        return dashboard.main([cmd, *rest])
    if cmd == "audit":
        return audit.main(rest)
    if cmd == "guard":
        return guard.main(rest)
    if cmd == "attack-path":
        return mcp_risk.main(rest)
    if cmd == "install":
        return install.main(rest)
    if cmd in ("version", "--version", "-V"):
        print(f"ctx (ctxprof) {__version__}")
        return 0

    print(f"unknown command: {cmd}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
