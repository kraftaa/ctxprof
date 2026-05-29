#!/usr/bin/env python3
"""Claude Code statusline heartbeat plus live multi-session dashboard.

Use as a Claude Code `statusLine` command to record each active session, then
run the same script in dashboard mode to view all recently active sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any


STATE_DIR = Path(os.environ.get("CLAUDE_STATUS_STATE_DIR", "~/.claude/session-status")).expanduser()


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


STALE_AFTER_SECONDS = int_env("CLAUDE_STATUS_STALE_AFTER_SECONDS", 900)

RESET = "\033[0m"
DIM = "\033[2m"
TEAL = "\033[38;5;37m"
GREEN = "\033[38;5;70m"
YELLOW = "\033[38;5;178m"
RED = "\033[38;5;203m"


def nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def first(data: dict[str, Any], paths: list[str], default: Any = None) -> Any:
    for path in paths:
        value = nested(data, path)
        if value not in (None, "", "null"):
            return value
    return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def maybe_number(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_int(value: Any) -> str:
    if value in (None, "", "null"):
        return "-"
    n = number(value)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def duration(ms: Any) -> str:
    seconds = int(number(ms) / 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def clean_session_id(raw: Any) -> str:
    text = str(raw or "unknown")
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)
    if len(safe) <= 96:
        return safe or "unknown"
    suffix = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:87]}_{suffix}"


def repo_name(path: Any) -> str:
    if not path:
        return "-"
    return Path(str(path)).name or str(path)


def extract_record(data: dict[str, Any]) -> dict[str, Any]:
    usage = first(data, ["context_window.current_usage", "usage"], {})
    if not isinstance(usage, dict):
        usage = {}

    input_tokens = maybe_number(first(
        usage,
        ["input_tokens", "prompt_tokens", "total_input_tokens"],
        first(data, ["context_window.current_usage.input_tokens"], None),
    ))
    output_tokens = maybe_number(first(
        usage,
        ["output_tokens", "completion_tokens", "total_output_tokens"],
        first(data, ["context_window.current_usage.output_tokens"], None),
    ))
    cache_read_tokens = maybe_number(first(
        usage,
        ["cache_read_input_tokens", "cache_read_tokens"],
        first(data, ["context_window.current_usage.cache_read_input_tokens"], None),
    ))
    cache_write_tokens = maybe_number(first(
        usage,
        ["cache_creation_input_tokens", "cache_creation_tokens", "cache_write_tokens"],
        first(data, ["context_window.current_usage.cache_creation_input_tokens"], None),
    ))

    usage_parts = [input_tokens, output_tokens, cache_read_tokens, cache_write_tokens]
    total_tokens = maybe_number(first(
        data,
        ["context_window.current_usage.total_tokens", "usage.total_tokens"],
        None,
    ))
    if total_tokens is None and any(part is not None for part in usage_parts):
        total_tokens = sum(part or 0 for part in usage_parts)

    cwd = first(data, ["workspace.current_dir", "cwd"], "")
    project_dir = first(data, ["workspace.project_dir", "cwd"], cwd)
    session_id = clean_session_id(first(data, ["session_id"], project_dir or cwd))

    return {
        "session_id": session_id,
        "updated_at": time.time(),
        "model": first(data, ["model.display_name", "model.id"], "-"),
        "agent": first(data, ["agent.name"], ""),
        "cwd": cwd,
        "project_dir": project_dir,
        "repo": repo_name(project_dir or cwd),
        "context_pct": maybe_number(first(data, ["context_window.used_percentage"], None)),
        "rate_pct": maybe_number(first(data, ["rate_limits.five_hour.used_percentage"], None)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "cost_usd": number(first(data, ["cost.total_cost_usd"], 0)),
        "duration_ms": number(first(data, ["cost.total_duration_ms"], 0)),
        "transcript_path": first(data, ["transcript_path"], ""),
    }


def record_path(session_id: str) -> Path:
    return STATE_DIR / f"{clean_session_id(session_id)}.json"


def write_record(record: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = record_path(record["session_id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    tmp.replace(record_path(record["session_id"]))


def load_records(include_stale: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    records: list[dict[str, Any]] = []
    if not STATE_DIR.exists():
        return records

    for path in STATE_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if include_stale or now - number(record.get("updated_at")) <= STALE_AFTER_SECONDS:
            records.append(record)

    return sorted(records, key=lambda item: number(item.get("updated_at")), reverse=True)


def statusline() -> int:
    try:
        data = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        print(f"{RED}Claude status unavailable{RESET}")
        return 1

    record = extract_record(data)
    try:
        write_record(record)
    except OSError:
        pass

    model = record["model"]
    repo = record["repo"]
    ctx = record["context_pct"]
    total = compact_int(record["total_tokens"])
    cost = record["cost_usd"]
    rate = record["rate_pct"]

    ctx_text = f"{ctx:.0f}%" if ctx is not None else "-"
    rate_text = f"{rate:.0f}%" if rate is not None else "-"
    ctx_color = GREEN if ctx is None or ctx < 60 else YELLOW if ctx < 80 else RED
    print(f"{TEAL}{model}{RESET} {YELLOW}{repo}{RESET} {ctx_color}{ctx_text}{RESET} tok:{total} ${cost:.2f} 5h:{rate_text}")
    return 0


def truncate(text: Any, width: int) -> str:
    value = str(text or "")
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def render_table(include_stale: bool = False) -> str:
    records = load_records(include_stale=include_stale)
    now = time.time()
    width = shutil.get_terminal_size((120, 30)).columns
    repo_width = max(14, min(28, width - 92))

    total_cost = sum(number(r.get("cost_usd")) for r in records)
    total_tokens = sum(number(r.get("total_tokens")) for r in records)
    max_ctx = max([number(r.get("context_pct")) for r in records if r.get("context_pct") is not None] or [0])
    max_rate = max([number(r.get("rate_pct")) for r in records if r.get("rate_pct") is not None] or [0])

    lines = [
        f"{TEAL}Claude sessions{RESET}  active:{len(records)}  tokens:{compact_int(total_tokens)}  cost:${total_cost:.2f}  max_ctx:{max_ctx:.0f}%  max_5h:{max_rate:.0f}%",
        "",
        (
            f"{DIM}{'AGE':>5}  {'MODEL':<12}  {'REPO':<{repo_width}}  {'CTX':>4}  {'TOKENS':>8}  "
            f"{'IN':>7}  {'OUT':>7}  {'CACHE R/W':>11}  {'COST':>7}  {'TIME':>7}  SESSION{RESET}"
        ),
    ]

    stale_count = 0
    for r in records:
        session = truncate(r.get("session_id"), max(8, width - repo_width - 95))
        cache = f"{compact_int(r.get('cache_read_tokens'))}/{compact_int(r.get('cache_write_tokens'))}"
        ctx = maybe_number(r.get("context_pct"))
        ctx_text = f"{ctx:>3.0f}%" if ctx is not None else "   -"
        ctx_color = RED if ctx is not None and ctx >= 80 else YELLOW if ctx is not None and ctx >= 60 else GREEN
        seconds_since = now - number(r.get("updated_at"))
        is_stale = seconds_since > STALE_AFTER_SECONDS
        age_text = f"{age(seconds_since)}!" if is_stale else age(seconds_since)
        if is_stale:
            stale_count += 1
        row = (
            f"{age_text:>5}  "
            f"{truncate(r.get('model'), 12):<12}  "
            f"{truncate(r.get('repo'), repo_width):<{repo_width}}  "
            f"{ctx_color}{ctx_text}{RESET}  "
            f"{compact_int(r.get('total_tokens')):>8}  "
            f"{compact_int(r.get('input_tokens')):>7}  "
            f"{compact_int(r.get('output_tokens')):>7}  "
            f"{cache:>11}  "
            f"${number(r.get('cost_usd')):>6.2f}  "
            f"{duration(r.get('duration_ms')):>7}  "
            f"{session}"
        )
        # Dim stale rows so live sessions stand out; the cumulative cost/time on
        # a stale row is a frozen session total, not ongoing spend.
        lines.append(f"{DIM}{row}{RESET}" if is_stale else row)

    if not records:
        lines.append("No active sessions yet. Start Claude Code with the heartbeat statusline configured.")
    else:
        lines.append("")
        notes = ["COST and TIME are cumulative session totals (not per-refresh)."]
        if stale_count:
            notes.append(f"{stale_count} stale (age!) session(s) shown — last heartbeat over {age(STALE_AFTER_SECONDS)} ago.")
        else:
            notes.append(f"Sessions idle over {age(STALE_AFTER_SECONDS)} are hidden (use --include-stale to show them).")
        lines.append(f"{DIM}{'  '.join(notes)}{RESET}")
        lines.append(f"{DIM}Inspect one: ctx show <session-id>{RESET}")

    return "\n".join(lines)


def dashboard(refresh_seconds: float, include_stale: bool) -> int:
    if refresh_seconds <= 0:
        print(render_table(include_stale=include_stale))
        return 0

    try:
        while True:
            print("\033[H\033[2J", end="")
            print(render_table(include_stale=include_stale))
            time.sleep(refresh_seconds)
    except KeyboardInterrupt:
        return 130


def find_record(session: str | None) -> dict[str, Any] | None:
    records = load_records(include_stale=True)
    if not records:
        return None
    if not session:
        return records[0]  # already sorted newest-first
    cleaned = clean_session_id(session)
    for record in records:
        sid = str(record.get("session_id", ""))
        if sid == cleaned or sid == session or sid.startswith(session):
            return record
    return None


def show(session: str | None) -> int:
    record = find_record(session)
    if not record:
        target = session or "newest active session"
        print(f"{RED}No heartbeat record found for {target} under {STATE_DIR}{RESET}")
        return 1

    now = time.time()
    seconds_since = now - number(record.get("updated_at"))
    stale = " (stale)" if seconds_since > STALE_AFTER_SECONDS else ""
    transcript = record.get("transcript_path") or ""

    def line(label: str, value: Any) -> str:
        return f"  {DIM}{label:<14}{RESET}{value}"

    out = [
        f"{TEAL}Session{RESET} {record.get('session_id')}",
        line("repo", record.get("repo") or "-"),
        line("model", record.get("model") or "-"),
        line("cwd", record.get("cwd") or "-"),
        line("last seen", f"{age(seconds_since)} ago{stale}"),
        line("context", f"{record.get('context_pct')}%  (size {compact_int(record.get('context_size'))})"),
        line("tokens", f"total {compact_int(record.get('total_tokens'))}  in {compact_int(record.get('input_tokens'))}  out {compact_int(record.get('output_tokens'))}"),
        line("cache R/W", f"{compact_int(record.get('cache_read_tokens'))} / {compact_int(record.get('cache_write_tokens'))}"),
        line("cost (cum)", f"${number(record.get('cost_usd')):.2f}"),
        line("time (cum)", duration(record.get("duration_ms"))),
        line("transcript", transcript or "(unknown — heartbeat had no transcript_path)"),
    ]
    print("\n".join(out))
    if transcript:
        print(f"\n{DIM}# Audit why this session burned context/cache:{RESET}")
        print(f"ctx audit --transcript {transcript}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctx", description="Claude Code multi-session status dashboard")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("statusline", help="read Claude status JSON from stdin and print one-line status")
    dash = subparsers.add_parser("dashboard", help="render all recently active Claude sessions")
    dash.add_argument("--refresh", type=float, default=1.0, help="refresh interval in seconds; 0 prints once")
    dash.add_argument("--include-stale", action="store_true", help="include sessions older than the stale timeout")
    show_parser = subparsers.add_parser("show", help="print details + ready-to-run audit command for one session")
    show_parser.add_argument("session", nargs="?", help="session id (or prefix); omitted = newest active session")

    args = parser.parse_args(argv)
    if args.command == "dashboard":
        return dashboard(args.refresh, args.include_stale)
    if args.command == "show":
        return show(args.session)
    return statusline()


if __name__ == "__main__":
    raise SystemExit(main())
