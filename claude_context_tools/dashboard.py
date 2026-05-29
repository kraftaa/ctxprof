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
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int, align: str = "l") -> str:
    gap = width - _visible_len(text)
    if gap <= 0:
        return text
    return text + " " * gap if align == "l" else " " * gap + text


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


CTX_BAR_W = 6  # context fill-bar width
# (key, header, fixed_width|None, align, priority). Lower priority = kept longer
# when the terminal is narrow. None width = flexible (repo, label).
COLUMNS = [
    ("age", "SEEN", 5, "r", 2),
    ("mode", "MODE", 11, "l", 6),
    ("agent", "AGENT", 10, "l", 9),
    ("repo", "REPO", None, "l", 1),
    ("context", "CONTEXT", CTX_BAR_W + 5, "l", 1),
    ("rate", "5H/7D", 7, "r", 8),
    ("tokens", "TOKENS", 8, "r", 3),
    ("in", "IN", 7, "r", 7),
    ("out", "OUT", 7, "r", 8),
    ("cache", "CACHE R/W", 11, "r", 5),
    ("cost", "COST", 8, "r", 4),
    ("api", "API", 6, "r", 9),
    ("time", "DUR", 7, "r", 6),
    ("chg", "CHG", 11, "r", 10),
    ("recent", "RECENT", SPARK_POINTS, "r", 3),
    ("session", "ID", 8, "l", 5),
    ("label", "LABEL", None, "l", 2),
]
_FLEX_MIN = {"repo": 10, "label": 12}
_FLEX_CAP = {"repo": 24, "label": 60}
_SEP = 2


def _mode_str(r: dict[str, Any]) -> str:
    effort = str(r.get("effort") or "")
    thinking = str(r.get("thinking") or "").lower() in ("true", "1", "yes")
    return (effort + ("+think" if thinking else "")) or "-"


def _chg_str(r: dict[str, Any]) -> str:
    added = number(r.get("lines_added"))
    removed = number(r.get("lines_removed"))
    if added <= 0 and removed <= 0:
        return "-"
    return f"+{compact_int(added)}/-{compact_int(removed)}"


def _layout_width(cols: list[tuple]) -> int:
    total = sum(w if w is not None else _FLEX_MIN[k] for k, _, w, _, _ in cols)
    return total + _SEP * max(0, len(cols) - 1)


def _select_columns(width: int) -> tuple[list[tuple], dict[str, int]]:
    """Pick columns that fit `width` (essentials always kept), then size flex cols."""
    chosen: list[tuple] = []
    for col in sorted(COLUMNS, key=lambda c: c[4]):
        if col[4] <= 1 or _layout_width(chosen + [col]) <= width:
            chosen.append(col)
    order = {c[0]: i for i, c in enumerate(COLUMNS)}
    chosen.sort(key=lambda c: order[c[0]])

    fixed = sum(w for _, _, w, _, _ in chosen if w is not None)
    leftover = width - fixed - _SEP * max(0, len(chosen) - 1)
    flex = [k for k, _, w, _, _ in chosen if w is None]
    widths: dict[str, int] = {}
    # Allocate repo first (capped), then give the rest to label.
    for key in ("repo", "label"):
        if key not in flex:
            continue
        reserve = sum(_FLEX_MIN[o] for o in flex if o != key and o not in widths)
        widths[key] = max(_FLEX_MIN[key], min(_FLEX_CAP[key], leftover - reserve))
        leftover -= widths[key]
    return chosen, widths


def _row_cells(r: dict[str, Any], now: float) -> tuple[dict[str, str], bool]:
    seconds_since = now - number(r.get("updated_at"))
    is_stale = seconds_since > STALE_AFTER_SECONDS
    ctx = maybe_number(r.get("context_pct"))
    ctx_color = RED if ctx is not None and ctx >= 80 else YELLOW if ctx is not None and ctx >= 60 else GREEN
    if ctx is not None:
        context = f"{ctx_color}{fill_bar(ctx / 100, CTX_BAR_W)}{RESET} {ctx:>3.0f}%"
    else:
        context = f"{'░' * CTX_BAR_W}    -"
    rate5 = maybe_number(r.get("rate_pct"))
    rate7 = maybe_number(r.get("seven_day_pct"))
    rate = f"{rate5:.0f}/{rate7:.0f}%" if rate5 is not None and rate7 is not None else "-"
    cells = {
        "age": f"{age(seconds_since)}!" if is_stale else age(seconds_since),
        "mode": _mode_str(r),
        "agent": truncate(r.get("agent") or "", 10),
        "repo": str(r.get("repo") or "-"),
        "context": context,
        "rate": rate,
        "tokens": compact_int(r.get("total_tokens")),
        "in": compact_int(r.get("input_tokens")),
        "out": compact_int(r.get("output_tokens")),
        "cache": f"{compact_int(r.get('cache_read_tokens'))}/{compact_int(r.get('cache_write_tokens'))}",
        "cost": f"${number(r.get('cost_usd')):.2f}",
        "api": duration(r.get("api_duration_ms")),
        "time": duration(r.get("duration_ms")),
        "chg": _chg_str(r),
        "recent": sparkline(str(r.get("session_id") or "")) or (" " * SPARK_POINTS),
        "session": str(r.get("session_id") or "-")[:8],
        "label": str(r.get("session_name") or "-"),
    }
    return cells, is_stale


def render_table(include_stale: bool = False) -> str:
    now = time.time()
    all_records = load_records(include_stale=True)
    if include_stale:
        records = all_records
    else:
        records = [r for r in all_records if now - number(r.get("updated_at")) <= STALE_AFTER_SECONDS]
    hidden = len(all_records) - len(records)

    width = shutil.get_terminal_size((140, 30)).columns
    columns, flex_w = _select_columns(width)

    def col_width(key: str, fixed: int | None) -> int:
        return flex_w[key] if fixed is None else fixed

    total_cost = sum(number(r.get("cost_usd")) for r in records)
    total_tokens = sum(number(r.get("total_tokens")) for r in records)
    max_ctx = max([number(r.get("context_pct")) for r in records if r.get("context_pct") is not None] or [0])
    max_rate = max([number(r.get("rate_pct")) for r in records if r.get("rate_pct") is not None] or [0])

    header_summary = (
        f"{TEAL}Claude sessions{RESET}  active:{len(records)}"
        + (f"  {DIM}stale-hidden:{hidden}{RESET}" if hidden and not include_stale else "")
        + f"  live-ctx:{compact_int(total_tokens)}  cost:${total_cost:.2f}  max_ctx:{max_ctx:.0f}%  max_5h:{max_rate:.0f}%"
    )
    header_row = "  ".join(
        _pad(header, col_width(key, w), align) for key, header, w, align, _ in columns
    )
    lines = [header_summary, "", f"{DIM}{header_row}{RESET}"]

    stale_count = 0
    shown_keys = {c[0] for c in columns}
    for r in records:
        cells, is_stale = _row_cells(r, now)
        if is_stale:
            stale_count += 1
        rendered = []
        for key, _, w, align, _ in columns:
            text = cells[key]
            cw = col_width(key, w)
            if key in ("repo", "label"):
                text = truncate(text, cw)
            rendered.append(_pad(text, cw, align))
        row = "  ".join(rendered)
        # Dim stale rows; their cumulative cost/time is frozen, not ongoing spend.
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
        if "recent" in shown_keys:
            lines.append(f"{DIM}RECENT = last {SPARK_POINTS} turns' token volume; {GREEN}green{DIM}=cached {CYAN}cyan{DIM}=fresh.{RESET}")
        lines.append(f"{DIM}Inspect one: ctx show <session-id>{RESET}")

    return "\n".join(lines)


def recent_steps(limit: int) -> list[dict[str, Any]]:
    """Most recent per-turn step records across all sessions, newest first."""
    steps_dir = STATE_DIR / "steps"
    if not steps_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in steps_dir.glob("*.jsonl"):
        for line in _tail_lines(path, limit):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda s: number(s.get("timestamp")), reverse=True)
    return rows[:limit]


# (key, header, width, align) for the per-turn steps feed.
STEP_COLUMNS = [
    ("age", "SEEN", 5, "r"),
    ("step", "STEP", 5, "r"),
    ("repo", "REPO", 22, "l"),
    ("context", "CTX", 4, "r"),
    ("tokens", "TOKENS", 7, "r"),
    ("cache", "CACHE R/W", 11, "r"),
    ("cost", "COST", 9, "r"),
    ("api", "APIΔ", 6, "r"),
    ("gap", "GAP", 6, "r"),
    ("label", "LABEL", 28, "l"),
]


def render_steps(limit: int) -> str:
    now = time.time()
    steps = recent_steps(limit)
    header = "  ".join(f"{_pad(h, w, a)}" for _, h, w, a in STEP_COLUMNS)
    lines = [
        f"{TEAL}Recent step costs{RESET}  (last {len(steps)} turns across all sessions)",
        "",
        f"{DIM}{header}{RESET}",
    ]
    for s in steps:
        cost = number(s.get("delta_cost_usd"))
        cost_color = RED if cost >= 1.0 else YELLOW if cost >= 0.25 else ""
        cells = {
            "age": age(now - number(s.get("timestamp"))),
            "step": str(int(number(s.get("step_no")))),
            "repo": truncate(s.get("repo") or "-", 22),
            "context": f"{number(s.get('context_pct')):.0f}%",
            "tokens": compact_int(s.get("delta_tokens")),
            "cache": f"{compact_int(s.get('delta_cache_read_tokens'))}/{compact_int(s.get('delta_cache_write_tokens'))}",
            "cost": f"{cost_color}${cost:.4f}{RESET}" if cost_color else f"${cost:.4f}",
            "api": duration(s.get("delta_api_duration_ms")),
            "gap": age(number(s.get("delta_seen_seconds"))),
            "label": truncate(s.get("session_name") or "-", 28),
        }
        lines.append("  ".join(_pad(cells[k], w, a) for k, _, w, a in STEP_COLUMNS))
    if not steps:
        lines.append("No step data yet — needs the heartbeat statusline's steps/<session>.jsonl files.")
    else:
        lines.append("")
        lines.append(f"{DIM}COST per turn; {YELLOW}yellow{DIM} >=$0.25, {RED}red{DIM} >=$1.00. GAP = wall time since the previous turn.{RESET}")
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
        line("label", record.get("session_name") or "-"),
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
    steps_parser = subparsers.add_parser("steps", help="recent per-turn cost feed across all sessions")
    steps_parser.add_argument("--limit", type=int, default=20, help="number of recent turns to show")

    args = parser.parse_args(argv)
    if args.command == "dashboard":
        return dashboard(args.refresh, args.include_stale)
    if args.command == "show":
        return show(args.session)
    if args.command == "steps":
        print(render_steps(max(1, args.limit)))
        return 0
    return statusline()


if __name__ == "__main__":
    raise SystemExit(main())
