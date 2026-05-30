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

# Emit ANSI color only to a real terminal. When piped/captured (e.g. `!ctx ...`
# inside Claude, or `ctx ... | less`), drop color so the output is clean text,
# not escape-code soup. FORCE_COLOR overrides; NO_COLOR disables.
_USE_COLOR = bool(os.environ.get("FORCE_COLOR")) or (
    sys.stdout.isatty() and not os.environ.get("NO_COLOR")
)


def _ansi(code: str) -> str:
    return code if _USE_COLOR else ""


RESET = _ansi("\033[0m")
DIM = _ansi("\033[2m")
TEAL = _ansi("\033[38;5;37m")
GREEN = _ansi("\033[38;5;70m")
YELLOW = _ansi("\033[38;5;178m")
RED = _ansi("\033[38;5;203m")
CYAN = _ansi("\033[38;5;38m")

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


def parse_since(value: str | None) -> float | None:
    """Parse a duration like '90s', '30m', '2h', '1d' into seconds. None if empty."""
    if not value:
        return None
    value = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    mult = units.get(value[-1])
    try:
        return float(value[:-1]) * mult if mult else float(value)
    except (ValueError, TypeError):
        return None


def step_view(s: dict[str, Any], now: float) -> dict[str, Any]:
    """Normalize a raw heartbeat step row into a stable, analyzable record."""
    return {
        "session_id": s.get("session_id"),
        "step": int(number(s.get("step_no"))),
        "repo": s.get("repo"),
        "label": s.get("session_name"),
        "timestamp": number(s.get("timestamp")),
        "age_s": round(now - number(s.get("timestamp")), 1),
        "context_pct": number(s.get("context_pct")),
        "tokens": number(s.get("delta_tokens")),
        "input": number(s.get("delta_input_tokens")),
        "output": number(s.get("delta_output_tokens")),
        "cache_read": number(s.get("delta_cache_read_tokens")),
        "cache_write": number(s.get("delta_cache_write_tokens")),
        "cost_usd": round(number(s.get("delta_cost_usd")), 6),
        "api_ms": number(s.get("delta_api_duration_ms")),
        "gap_s": number(s.get("delta_seen_seconds")),
        "lines_added": number(s.get("delta_lines_added")),
        "lines_removed": number(s.get("delta_lines_removed")),
    }


def collect_steps(limit: int, since_seconds: float | None) -> list[dict[str, Any]]:
    now = time.time()
    views = [step_view(s, now) for s in recent_steps(limit)]
    if since_seconds is not None:
        views = [v for v in views if v["age_s"] <= since_seconds]
    return views


def build_digest(views: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up step records into the 'what mattered' summary."""
    by_session: dict[str, dict[str, Any]] = {}
    for v in views:
        sid = str(v.get("session_id") or "-")
        agg = by_session.setdefault(sid, {"session_id": sid, "label": v.get("label"),
                                          "repo": v.get("repo"), "turns": 0, "cost_usd": 0.0, "tokens": 0.0})
        agg["turns"] += 1
        agg["cost_usd"] += v["cost_usd"]
        agg["tokens"] += v["tokens"]
    top = lambda key, n=8: sorted(views, key=lambda v: v[key], reverse=True)[:n]
    return {
        "turns": len(views),
        "sessions": len(by_session),
        "total_cost_usd": round(sum(v["cost_usd"] for v in views), 4),
        "total_tokens": sum(v["tokens"] for v in views),
        "top_cost_turns": top("cost_usd"),
        "biggest_cache_writes": [v for v in top("cache_write") if v["cache_write"] > 0],
        "longest_gaps": top("gap_s"),
        "by_session": sorted(by_session.values(), key=lambda a: a["cost_usd"], reverse=True),
    }


def render_digest(limit: int, since: str | None, as_json: bool) -> str:
    since_seconds = parse_since(since)
    views = collect_steps(limit, since_seconds)
    digest = build_digest(views)
    if as_json:
        return json.dumps(digest, indent=2, sort_keys=True)
    if not views:
        return "No step data yet — needs the heartbeat statusline's steps/<session>.jsonl files."

    window = f"last {since}" if since else f"last {len(views)} turns"
    lines = [
        f"{TEAL}Step digest{RESET}  ({window} across {digest['sessions']} session(s))",
        f"  totals: ${digest['total_cost_usd']:.2f}  {compact_int(digest['total_tokens'])} tok  {digest['turns']} turns",
        "",
        f"{DIM}Per session (by cost):{RESET}",
    ]
    for a in digest["by_session"]:
        lines.append(f"  ${a['cost_usd']:>7.2f}  {a['turns']:>4} turns  {compact_int(a['tokens']):>7}  "
                     f"{truncate(a['repo'] or '-', 18):<18}  {truncate(a['label'] or '-', 40)}")
    lines.append(f"\n{DIM}Top-cost turns:{RESET}")
    for v in digest["top_cost_turns"]:
        c = v["cost_usd"]
        col = RED if c >= 1.0 else YELLOW if c >= 0.25 else ""
        lines.append(f"  {col}${c:>7.4f}{RESET if col else ''}  step {v['step']:>5}  ctx {v['context_pct']:>3.0f}%  "
                     f"{truncate(v['repo'] or '-', 20)}")
    lines.append(f"\n{DIM}Biggest cache writes (likely invalidation):{RESET}")
    for v in digest["biggest_cache_writes"]:
        lines.append(f"  {compact_int(v['cache_write']):>8} tok  step {v['step']:>5}  read {compact_int(v['cache_read'])}  "
                     f"{truncate(v['repo'] or '-', 20)}")
    lines.append(f"\n{DIM}Longest idle gaps before a turn:{RESET}")
    for v in digest["longest_gaps"]:
        lines.append(f"  {age(v['gap_s']):>6}  step {v['step']:>5}  {truncate(v['repo'] or '-', 20)}")
    return "\n".join(lines)


def tui(limit: int) -> int:
    """Scrollable/filterable curses browser for the per-turn step feed."""
    if not sys.stdout.isatty():
        print(f"{RED}ctx tui needs an interactive terminal (try `ctx steps` / `ctx digest` instead).{RESET}")
        return 1
    try:
        import curses
    except ImportError:
        print(f"{RED}curses is unavailable on this platform; use `ctx steps`.{RESET}")
        return 1

    now = time.time()
    all_views = [step_view(s, now) for s in recent_steps(limit)]

    def run(stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        for i in range(1, 5):
            curses.init_pair(i, [curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN][i - 1], -1)
        GREENP, YELLOWP, REDP, CYANP = 1, 2, 3, 4
        sel, top, filt, typing = 0, 0, "", False
        while True:
            views = [v for v in all_views if not filt or filt.lower() in str(v.get("repo") or "").lower()
                     or filt.lower() in str(v.get("label") or "").lower()]
            sel = max(0, min(sel, len(views) - 1)) if views else 0
            h, w = stdscr.getmaxyx()
            body = h - 4
            if sel < top:
                top = sel
            elif sel >= top + body:
                top = sel - body + 1
            stdscr.erase()
            stdscr.addnstr(0, 0, f" ctx tui — {len(views)} turns   "
                           f"[↑↓/jk move  / filter  enter=detail  q quit]", w - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, f" {'SEEN':>5}  {'STEP':>5}  {'REPO':<18}  {'CTX':>4}  {'TOKENS':>7}  "
                           f"{'CACHE R/W':>11}  {'COST':>9}  {'GAP':>6}  LABEL", w - 1, curses.A_DIM)
            for idx in range(top, min(len(views), top + body)):
                v = views[idx]
                c = v["cost_usd"]
                line = (f" {age(now - v['timestamp']):>5}  {v['step']:>5}  {truncate(v['repo'] or '-', 18):<18}  "
                        f"{v['context_pct']:>3.0f}%  {compact_int(v['tokens']):>7}  "
                        f"{compact_int(v['cache_read'])}/{compact_int(v['cache_write']):>11}  "
                        f"${c:>7.4f}  {age(v['gap_s']):>6}  {truncate(v['label'] or '-', max(4, w - 70))}")
                attr = curses.A_REVERSE if idx == sel else 0
                if c >= 1.0:
                    attr |= curses.color_pair(REDP)
                elif c >= 0.25:
                    attr |= curses.color_pair(YELLOWP)
                stdscr.addnstr(2 + idx - top, 0, line, w - 1, attr)
            status = f" filter: {filt}_" if typing else (f" filter: {filt}  (/ to change)" if filt else "")
            stdscr.addnstr(h - 1, 0, status, w - 1, curses.A_DIM)
            stdscr.refresh()

            ch = stdscr.getch()
            if typing:
                if ch in (10, 13):
                    typing = False
                elif ch in (27,):
                    typing, filt = False, ""
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    filt = filt[:-1]
                elif 32 <= ch < 127:
                    filt += chr(ch)
                continue
            if ch in (ord("q"), 27):
                break
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel += 1
            elif ch in (curses.KEY_UP, ord("k")):
                sel -= 1
            elif ch == curses.KEY_NPAGE:
                sel += body
            elif ch == curses.KEY_PPAGE:
                sel -= body
            elif ch == ord("/"):
                typing, filt = True, ""
            elif ch in (10, 13) and views:
                _tui_detail(stdscr, views[sel], now)

    def _tui_detail(stdscr, v, now):
        h, w = stdscr.getmaxyx()
        rows = [
            f"step {v['step']}  —  {v.get('repo') or '-'}",
            f"label    {v.get('label') or '-'}",
            f"session  {v.get('session_id') or '-'}",
            f"seen     {age(now - v['timestamp'])} ago   gap before {age(v['gap_s'])}",
            f"context  {v['context_pct']:.0f}%",
            f"tokens   total {compact_int(v['tokens'])}  in {compact_int(v['input'])}  out {compact_int(v['output'])}",
            f"cache    read {compact_int(v['cache_read'])}  write {compact_int(v['cache_write'])}",
            f"cost     ${v['cost_usd']:.4f}   api {duration(v['api_ms'])}",
            f"lines    +{int(v['lines_added'])}/-{int(v['lines_removed'])}",
            "",
            "press any key to go back",
        ]
        stdscr.erase()
        for i, line in enumerate(rows):
            if i < h - 1:
                stdscr.addnstr(i, 0, line, w - 1)
        stdscr.refresh()
        stdscr.getch()

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0


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
    steps_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON (great for `!ctx steps --json` inside Claude)")
    digest_parser = subparsers.add_parser("digest", help="rollup summary of recent turns (cost, cache writes, gaps)")
    digest_parser.add_argument("--limit", type=int, default=1000, help="max recent turns per session to scan")
    digest_parser.add_argument("--since", help="only turns newer than e.g. 30m, 2h, 1d")
    digest_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    tui_parser = subparsers.add_parser("tui", help="interactive scrollable/filterable step browser")
    tui_parser.add_argument("--limit", type=int, default=500, help="number of recent turns to load")

    args = parser.parse_args(argv)
    if args.command == "dashboard":
        return dashboard(args.refresh, args.include_stale)
    if args.command == "show":
        return show(args.session)
    if args.command == "steps":
        if args.json:
            now = time.time()
            print(json.dumps([step_view(s, now) for s in recent_steps(max(1, args.limit))], indent=2))
        else:
            print(render_steps(max(1, args.limit)))
        return 0
    if args.command == "digest":
        print(render_digest(max(1, args.limit), args.since, args.json))
        return 0
    if args.command == "tui":
        return tui(max(1, args.limit))
    return statusline()


if __name__ == "__main__":
    raise SystemExit(main())
