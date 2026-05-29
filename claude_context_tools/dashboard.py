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
CYAN = "\033[38;5;38m"

SPARK_BLOCKS = "▁▂▃▄▆█"
SPARK_POINTS = 12


def fill_bar(fraction: float, width: int = 6) -> str:
    """A solid █/░ bar for a 0..1 fraction (used for context fill)."""
    fraction = 0.0 if fraction < 0 else 1.0 if fraction > 1 else fraction
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def _tail_lines(path: Path, n: int) -> list[str]:
    """Read roughly the last n lines of a file without loading the whole thing."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            # Step lines are ~800 bytes each; over-read so we reliably get n full lines.
            block = min(size, max(4096, n * 1200))
            handle.seek(size - block)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def sparkline(session_id: str, points: int = SPARK_POINTS) -> str:
    """Recent per-turn token bars, colored by cache share (green=cached, cyan=fresh).

    Mirrors the heartbeat statusline's sparkline so the dashboard shows the same
    at-a-glance activity per session. Returns "" when no step data exists.
    """
    path = STATE_DIR / "steps" / f"{session_id}.jsonl"
    if not path.exists():
        return ""
    totals: list[float] = []
    shares: list[float] = []
    for line in _tail_lines(path, points):
        line = line.strip()
        if not line:
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue
        inp = number(step.get("delta_input_tokens"))
        out = number(step.get("delta_output_tokens"))
        cache = number(step.get("delta_cache_read_tokens")) + number(step.get("delta_cache_write_tokens"))
        fresh = max(0.0, inp - cache)
        total = fresh + out + cache
        totals.append(total)
        shares.append(cache / total if total > 0 else 0.0)
    if not totals:
        return ""
    peak = max(totals) or 1.0
    cells = []
    for total, share in zip(totals, shares):
        ratio = total / peak
        idx = min(len(SPARK_BLOCKS) - 1, int(ratio * (len(SPARK_BLOCKS) - 1) + 0.999)) if ratio > 0 else 0
        color = GREEN if share >= 0.75 else YELLOW if share >= 0.25 else CYAN
        cells.append(f"{color}{SPARK_BLOCKS[idx]}{RESET}")
    pad = points - len(cells)
    return (" " * pad) + "".join(cells)


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
    now = time.time()
    all_records = load_records(include_stale=True)
    if include_stale:
        records = all_records
    else:
        records = [r for r in all_records if now - number(r.get("updated_at")) <= STALE_AFTER_SECONDS]
    hidden = len(all_records) - len(records)

    width = shutil.get_terminal_size((140, 30)).columns
    repo_width = max(12, min(24, width - 116))
    ctx_w = 6  # context fill bar width

    total_cost = sum(number(r.get("cost_usd")) for r in records)
    total_tokens = sum(number(r.get("total_tokens")) for r in records)
    max_ctx = max([number(r.get("context_pct")) for r in records if r.get("context_pct") is not None] or [0])
    max_rate = max([number(r.get("rate_pct")) for r in records if r.get("rate_pct") is not None] or [0])

    header_summary = (
        f"{TEAL}Claude sessions{RESET}  active:{len(records)}"
        + (f"  {DIM}stale-hidden:{hidden}{RESET}" if hidden and not include_stale else "")
        + f"  live-ctx:{compact_int(total_tokens)}  cost:${total_cost:.2f}  max_ctx:{max_ctx:.0f}%  max_5h:{max_rate:.0f}%"
    )
    lines = [
        header_summary,
        "",
        (
            f"{DIM}{'AGE':>5}  {'MODEL':<12}  {'REPO':<{repo_width}}  {'CONTEXT':<{ctx_w + 5}}  {'TOKENS':>8}  "
            f"{'IN':>7}  {'OUT':>7}  {'CACHE R/W':>11}  {'COST':>8}  {'TIME':>7}  {'RECENT':>{SPARK_POINTS}}  SESSION{RESET}"
        ),
    ]

    stale_count = 0
    for r in records:
        session = truncate(r.get("session_id"), max(8, width - repo_width - 120))
        cache = f"{compact_int(r.get('cache_read_tokens'))}/{compact_int(r.get('cache_write_tokens'))}"
        ctx = maybe_number(r.get("context_pct"))
        ctx_color = RED if ctx is not None and ctx >= 80 else YELLOW if ctx is not None and ctx >= 60 else GREEN
        if ctx is not None:
            ctx_cell = f"{ctx_color}{fill_bar(ctx / 100, ctx_w)}{RESET} {ctx:>3.0f}%"
        else:
            ctx_cell = f"{'░' * ctx_w}    -"
        seconds_since = now - number(r.get("updated_at"))
        is_stale = seconds_since > STALE_AFTER_SECONDS
        age_text = f"{age(seconds_since)}!" if is_stale else age(seconds_since)
        if is_stale:
            stale_count += 1
        spark = sparkline(str(r.get("session_id") or "")) or (" " * SPARK_POINTS)
        row = (
            f"{age_text:>5}  "
            f"{truncate(r.get('model'), 12):<12}  "
            f"{truncate(r.get('repo'), repo_width):<{repo_width}}  "
            f"{ctx_cell}  "
            f"{compact_int(r.get('total_tokens')):>8}  "
            f"{compact_int(r.get('input_tokens')):>7}  "
            f"{compact_int(r.get('output_tokens')):>7}  "
            f"{cache:>11}  "
            f"${number(r.get('cost_usd')):>7.2f}  "
            f"{duration(r.get('duration_ms')):>7}  "
            f"{spark}  "
            f"{session}"
        )
        # Dim stale rows so live sessions stand out; the cumulative cost/time on
        # a stale row is a frozen session total, not ongoing spend.
        lines.append(f"{DIM}{row}{RESET}" if is_stale else row)

    if not records:
        lines.append("No active sessions yet. Start Claude Code with the heartbeat statusline configured.")
    else:
        lines.append("")
        notes = ["COST/TIME are cumulative session totals; live-ctx/IN/OUT are the current context, not lifetime."]
        if stale_count:
            notes.append(f"{stale_count} stale (age!) shown.")
        elif hidden:
            notes.append(f"{hidden} idle >{age(STALE_AFTER_SECONDS)} hidden (use --include-stale).")
        lines.append(f"{DIM}{'  '.join(notes)}{RESET}")
        lines.append(f"{DIM}RECENT = last {SPARK_POINTS} turns' token volume; {GREEN}green{DIM}=cached {CYAN}cyan{DIM}=fresh. Inspect: ctx show <session-id>{RESET}")

    return "\n".join(lines)


def dashboard(refresh_seconds: float, include_stale: bool) -> int:
    # Print once if a one-shot was requested, OR if stdout is not an interactive
    # terminal (piped, captured, or run via a non-tty runner) — looping there
    # can't clear the screen and would just spam frame after frame.
    if refresh_seconds <= 0 or not sys.stdout.isatty():
        print(render_table(include_stale=include_stale))
        return 0

    # Live mode in a real terminal: use the alternate screen buffer so each frame
    # redraws in place and the user's scrollback is left untouched on exit.
    sys.stdout.write("\033[?1049h\033[?25l")  # enter alt screen, hide cursor
    sys.stdout.flush()
    try:
        while True:
            sys.stdout.write("\033[H\033[2J")  # home + clear
            sys.stdout.write(render_table(include_stale=include_stale) + "\n")
            sys.stdout.write(f"{DIM}(refresh {refresh_seconds:g}s — Ctrl-C to quit){RESET}\n")
            sys.stdout.flush()
            time.sleep(refresh_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")  # show cursor, leave alt screen
        sys.stdout.flush()


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
