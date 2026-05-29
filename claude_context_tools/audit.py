#!/usr/bin/env python3
"""Audit a Claude Code transcript for context and cache waste.

This is an offline companion to the live statusline dashboard. It reads a
Claude Code transcript JSONL plus optional status heartbeat step data and
reports which transcript categories are consuming the most space.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path(os.environ.get("CLAUDE_STATUS_STATE_DIR", "~/.claude/session-status")).expanduser()
LOG_HINT = re.compile(
    r"(Traceback|Exception|ERROR|WARN|FAIL|FAILED|npm ERR!|panic:|stack trace|"
    r"^\s*at\s+\S+\(|^\s*\d+\)|^\s*[EF]\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def compact_int(value: float | int | None) -> str:
    if value is None:
        return "-"
    n = float(value)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def pct(part: float, whole: float) -> str:
    if whole <= 0:
        return "0%"
    return f"{(part / whole) * 100:.0f}%"


def est_tokens(chars: int) -> int:
    return max(1, round(chars / 4)) if chars else 0


def shorten(value: Any, width: int = 96) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"warning: skipped invalid JSON at {path}:{line_no}: {exc}", file=sys.stderr)
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def attachment_category(attachment: Any) -> str:
    if isinstance(attachment, dict):
        kind = attachment.get("type") or "unknown"
        return f"attachment:{kind}"
    return "attachments"


def find_status_for_transcript(transcript: Path, state_dir: Path) -> dict[str, Any] | None:
    if not state_dir.exists():
        return None
    transcript_resolved = str(transcript.expanduser())
    for path in state_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("transcript_path") == transcript_resolved:
            return record
    return None


def newest_status(state_dir: Path) -> dict[str, Any] | None:
    if not state_dir.exists():
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in state_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("transcript_path"):
            candidates.append((number(record.get("updated_at")), record))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def resolve_target(target: str | None, state_dir: Path) -> tuple[Path, dict[str, Any] | None]:
    if not target:
        status = newest_status(state_dir)
        if not status:
            raise SystemExit(f"no active status records with transcript_path under {state_dir}")
        return Path(status["transcript_path"]).expanduser(), status

    raw = Path(target).expanduser()
    if raw.exists() and raw.suffix == ".jsonl":
        return raw, find_status_for_transcript(raw, state_dir)

    if raw.exists() and raw.suffix == ".json":
        status = json.loads(raw.read_text(encoding="utf-8"))
        if not status.get("transcript_path"):
            raise SystemExit(f"status file has no transcript_path: {raw}")
        return Path(status["transcript_path"]).expanduser(), status

    status_path = state_dir / f"{target}.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if not status.get("transcript_path"):
            raise SystemExit(f"status file has no transcript_path: {status_path}")
        return Path(status["transcript_path"]).expanduser(), status

    raise SystemExit(f"target is not a transcript, status JSON, or session id: {target}")


@dataclass
class ToolCall:
    name: str
    label: str
    input_chars: int


def tool_label(name: str, input_data: Any) -> str:
    if not isinstance(input_data, dict):
        return name
    if name == "Read":
        return str(input_data.get("file_path") or input_data.get("path") or "Read")
    if name in {"Bash", "Shell"}:
        return shorten(input_data.get("command") or input_data.get("cmd") or name, 120)
    if name == "Agent":
        return shorten(input_data.get("description") or input_data.get("prompt") or "Agent", 120)
    return shorten(input_data.get("description") or input_data.get("prompt") or name, 120)


def load_steps(status: dict[str, Any] | None, state_dir: Path) -> list[dict[str, Any]]:
    if not status or not status.get("session_id"):
        return []
    path = state_dir / "steps" / f"{status['session_id']}.jsonl"
    if not path.exists():
        return []
    return read_jsonl(path)


# Once this many cache-read tokens have accumulated we treat the prompt prefix
# as "warm": further large cache writes are more likely invalidation than the
# normal one-time warm-up.
CACHE_WARM_THRESHOLD = 5000
# Cache write delta (tokens) below which a turn is too small to flag at all.
CACHE_WRITE_FLOOR = 2000


def analyze_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per-turn token deltas and classify cache write spikes.

    Heuristic only. The statusline heartbeat exposes per-turn token *counts*
    (input/output/cache-read/cache-write deltas), not individual cache-block
    boundaries, so this cannot prove which block broke. It separates the
    expected initial cache warm-up (large writes before any cache reads exist)
    from likely mid-session invalidation (large writes *after* the cache was
    already warm, with a low cache-read share that turn).
    """
    totals: Counter[str] = Counter()
    warmups: list[dict[str, Any]] = []
    invalidations: list[dict[str, Any]] = []
    cum_cache_read = 0.0

    for step in steps:
        delta_input = number(step.get("delta_input_tokens"))
        delta_output = number(step.get("delta_output_tokens"))
        delta_cache_read = number(step.get("delta_cache_read_tokens"))
        delta_cache_write = number(step.get("delta_cache_write_tokens"))
        delta_tokens = number(step.get("delta_tokens"))
        totals["input"] += delta_input
        totals["output"] += delta_output
        totals["cache_read"] += delta_cache_read
        totals["cache_write"] += delta_cache_write
        totals["tokens"] += delta_tokens

        cache_total = delta_cache_read + delta_cache_write
        read_share = delta_cache_read / cache_total if cache_total > 0 else 0.0
        write_share = delta_cache_write / cache_total if cache_total > 0 else 0.0
        # total_input_tokens includes cache *reads* + fresh input; cache *writes*
        # (creation) are billed separately and are NOT part of input. So fresh
        # genuinely-new input = input - cache_read (not - cache_read - cache_write).
        fresh = max(0.0, delta_input - delta_cache_read)
        was_warm = cum_cache_read >= CACHE_WARM_THRESHOLD

        if delta_cache_write >= CACHE_WRITE_FLOOR:
            event = {
                "step": int(number(step.get("step_no"))),
                "input": delta_input,
                "fresh": fresh,
                "cache_read": delta_cache_read,
                "cache_write": delta_cache_write,
                "read_share": read_share,
                "write_share": write_share,
                "context": step.get("context_pct"),
            }
            if was_warm and read_share < 0.5:
                invalidations.append(event)
            elif not was_warm:
                warmups.append(event)
        cum_cache_read += delta_cache_read

    invalidations.sort(key=lambda item: item["cache_write"], reverse=True)
    warmups.sort(key=lambda item: item["cache_write"], reverse=True)
    cacheable = totals["cache_read"] + totals["cache_write"]
    cache_read_share = totals["cache_read"] / cacheable if cacheable > 0 else None
    return {
        "step_totals": totals,
        "cache_warmups": warmups,
        "cache_invalidations": invalidations,
        "cache_read_share": cache_read_share,
    }


def analyze(rows: list[dict[str, Any]], status: dict[str, Any] | None, steps: list[dict[str, Any]]) -> dict[str, Any]:
    category_chars: Counter[str] = Counter()
    role_chars: Counter[str] = Counter()
    tool_calls: dict[str, ToolCall] = {}
    tool_result_chars: Counter[str] = Counter()
    tool_result_counts: Counter[str] = Counter()
    tool_input_chars: Counter[str] = Counter()
    read_counts: Counter[str] = Counter()
    read_chars: Counter[str] = Counter()
    command_counts: Counter[str] = Counter()
    command_chars: Counter[str] = Counter()
    duplicate_hashes: dict[str, dict[str, Any]] = {}
    large_results: list[tuple[int, str, str]] = []
    large_user_logs: list[tuple[int, str]] = []
    agent_reports: list[tuple[int, str]] = []
    text_blocks = 0
    tool_results = 0
    tool_uses = 0

    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        role = str(message.get("role") or row.get("type") or "unknown")
        content = message.get("content")

        if isinstance(content, str):
            size = len(content)
            text_blocks += 1
            role_chars[role] += size
            category_chars[f"{role} text"] += size
            if role == "user" and size >= 4000 and LOG_HINT.search(content):
                large_user_logs.append((size, shorten(content, 100)))
            continue

        if not isinstance(content, list):
            attachment = row.get("attachment")
            if attachment:
                size = len(content_text(attachment))
                category_chars[attachment_category(attachment)] += size
                role_chars["attachment"] += size
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = content_text(block.get("text"))
                size = len(text)
                text_blocks += 1
                role_chars[role] += size
                category_chars[f"{role} text"] += size
            elif block_type == "thinking":
                text = content_text(block.get("thinking") or block.get("text"))
                category_chars["assistant thinking"] += len(text)
            elif block_type == "tool_use":
                tool_uses += 1
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "tool")
                input_data = block.get("input")
                input_size = len(content_text(input_data))
                label = tool_label(name, input_data)
                tool_calls[tool_id] = ToolCall(name=name, label=label, input_chars=input_size)
                tool_input_chars[name] += input_size
                category_chars["tool call inputs"] += input_size
                if name == "Read":
                    read_counts[label] += 1
                elif name in {"Bash", "Shell"}:
                    command_counts[label] += 1
            elif block_type == "tool_result":
                tool_results += 1
                tool_id = str(block.get("tool_use_id") or "")
                call = tool_calls.get(tool_id, ToolCall(name="tool_result", label="unknown tool", input_chars=0))
                text = content_text(block.get("content"))
                size = len(text)
                category = f"{call.name} results"
                category_chars[category] += size
                tool_result_chars[call.name] += size
                tool_result_counts[call.name] += 1
                digest = sha(text.strip())
                if digest not in duplicate_hashes:
                    duplicate_hashes[digest] = {"size": size, "count": 0, "label": call.label, "tool": call.name}
                duplicate_hashes[digest]["count"] += 1
                if call.name == "Read":
                    read_chars[call.label] += size
                elif call.name in {"Bash", "Shell"}:
                    command_chars[call.label] += size
                elif call.name == "Agent":
                    agent_reports.append((size, call.label))
                if size >= 8000:
                    large_results.append((size, call.name, call.label))

    duplicate_waste = [
        item for item in duplicate_hashes.values()
        if item["count"] > 1 and item["size"] >= 200
    ]
    duplicate_waste.sort(key=lambda item: item["size"] * (item["count"] - 1), reverse=True)

    repeated_reads = [
        (read_counts[path], read_chars[path], path)
        for path in read_counts
        if read_counts[path] > 1
    ]
    repeated_reads.sort(key=lambda item: item[0] * item[1], reverse=True)

    repeated_commands = [
        (command_counts[cmd], command_chars[cmd], cmd)
        for cmd in command_counts
        if command_counts[cmd] > 1
    ]
    repeated_commands.sort(key=lambda item: item[0] * item[1], reverse=True)

    large_results.sort(reverse=True)
    large_user_logs.sort(reverse=True)
    agent_reports.sort(reverse=True)

    step_analysis = analyze_steps(steps)

    total_chars = sum(category_chars.values())
    return {
        "category_chars": category_chars,
        "role_chars": role_chars,
        "tool_result_chars": tool_result_chars,
        "tool_result_counts": tool_result_counts,
        "tool_input_chars": tool_input_chars,
        "repeated_reads": repeated_reads,
        "repeated_commands": repeated_commands,
        "duplicate_waste": duplicate_waste,
        "large_results": large_results,
        "large_user_logs": large_user_logs,
        "agent_reports": agent_reports,
        "text_blocks": text_blocks,
        "tool_results": tool_results,
        "tool_uses": tool_uses,
        "total_chars": total_chars,
        "status": status,
        "steps": steps,
        "step_totals": step_analysis["step_totals"],
        "cache_warmups": step_analysis["cache_warmups"],
        "cache_invalidations": step_analysis["cache_invalidations"],
        "cache_read_share": step_analysis["cache_read_share"],
    }


def print_top_counter(title: str, counter: Counter[str], total: int, limit: int) -> None:
    print(f"\n{title}:")
    if not counter:
        print("- none")
        return
    for name, chars in counter.most_common(limit):
        print(f"- {name}: ~{compact_int(est_tokens(chars))} tok ({pct(chars, total)})")


def print_report(transcript: Path, analysis: dict[str, Any], limit: int) -> None:
    status = analysis["status"] or {}
    total_chars = analysis["total_chars"]
    total_est = est_tokens(total_chars)
    repo = status.get("repo") or "-"
    session_id = status.get("session_id") or "-"

    print("Claude Context Audit")
    print(f"Transcript: {transcript}")
    print(f"Repo: {repo}  Session: {session_id}")
    print(f"Transcript payload estimate: ~{compact_int(total_est)} tokens from {compact_int(total_chars)} chars")
    if status:
        print(
            "Live/session totals: "
            f"ctx {status.get('context_pct', '-')}%  "
            f"tokens {compact_int(status.get('total_tokens'))}  "
            f"cache R/W {compact_int(status.get('cache_read_tokens'))}/{compact_int(status.get('cache_write_tokens'))}  "
            f"cost ${number(status.get('cost_usd')):.2f}"
        )

    print_top_counter("What burned context", analysis["category_chars"], total_chars, limit)

    attachments = {
        name: chars for name, chars in analysis["category_chars"].items()
        if name.startswith("attachment")
    }
    print("\nAttachment breakdown (system-injected context: skills, hooks, tool lists):")
    if attachments:
        attach_total = sum(attachments.values())
        print(f"- attachments total: ~{compact_int(est_tokens(attach_total))} tok ({pct(attach_total, total_chars)} of payload)")
        for name, chars in sorted(attachments.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
            print(f"- {name}: ~{compact_int(est_tokens(chars))} tok")
    else:
        print("- none detected")

    print_top_counter("Tool result weight", analysis["tool_result_chars"], sum(analysis["tool_result_chars"].values()), limit)

    print("\nRepeated file reads:")
    if analysis["repeated_reads"]:
        for count, chars, path in analysis["repeated_reads"][:limit]:
            print(f"- {count}x {path}: ~{compact_int(est_tokens(chars))} result tok")
    else:
        print("- none detected")

    print("\nRepeated commands:")
    if analysis["repeated_commands"]:
        for count, chars, cmd in analysis["repeated_commands"][:limit]:
            print(f"- {count}x {cmd}: ~{compact_int(est_tokens(chars))} result tok")
    else:
        print("- none detected")

    print("\nRepeated tool output blobs:")
    if analysis["duplicate_waste"]:
        for item in analysis["duplicate_waste"][:limit]:
            waste = item["size"] * (item["count"] - 1)
            print(
                f"- {item['count']}x {item['tool']} {shorten(item['label'], 72)}: "
                f"~{compact_int(est_tokens(waste))} repeated tok"
            )
    else:
        print("- none detected")

    print("\nLarge pasted logs / large tool results:")
    shown = 0
    for size, tool, label in analysis["large_results"][:limit]:
        shown += 1
        print(f"- {tool} {shorten(label, 80)}: ~{compact_int(est_tokens(size))} tok")
    for size, snippet in analysis["large_user_logs"][: max(0, limit - shown)]:
        print(f"- user paste {snippet}: ~{compact_int(est_tokens(size))} tok")
        shown += 1
    if shown == 0:
        print("- none over threshold")

    print("\nSubagent report weight:")
    if analysis["agent_reports"]:
        total_agent = sum(size for size, _ in analysis["agent_reports"])
        print(f"- total Agent result text: ~{compact_int(est_tokens(total_agent))} tok")
        for size, label in analysis["agent_reports"][:limit]:
            print(f"- {shorten(label, 80)}: ~{compact_int(est_tokens(size))} tok")
    else:
        print("- no Agent tool reports detected")

    print("\nCache telemetry (heuristic — token-level deltas, not exact cache-block attribution):")
    steps = analysis["steps"]
    if not steps:
        print("- no per-turn step file found; per-turn cache behavior is unknown (not zero)")
        print("  (this needs the heartbeat statusline's steps/<session>.jsonl; session-level R/W only is below)")
    else:
        totals = analysis["step_totals"]
        share = analysis["cache_read_share"]
        share_text = f"{share * 100:.0f}%" if share is not None else "unknown"
        print(
            f"- steps: {len(steps)}  input {compact_int(totals['input'])}  output {compact_int(totals['output'])}  "
            f"cache R/W {compact_int(totals['cache_read'])}/{compact_int(totals['cache_write'])}  "
            f"cache-read share {share_text}"
        )
        warmups = analysis["cache_warmups"]
        if warmups:
            warm_total = sum(item["cache_write"] for item in warmups)
            print(
                f"- initial cache warm-up (expected): {len(warmups)} turn(s), "
                f"~{compact_int(warm_total)} tok written before the cache was warm"
            )
        invalidations = analysis["cache_invalidations"]
        if invalidations:
            print("- likely cache invalidation (cache was warm, then largely rewritten):")
            for item in invalidations[:limit]:
                print(
                    f"  step {item['step']}: fresh {compact_int(item['fresh'])}, "
                    f"cache read {compact_int(item['cache_read'])}, write {compact_int(item['cache_write'])}, "
                    f"read share {item['read_share'] * 100:.0f}%, write share {item['write_share'] * 100:.0f}%, "
                    f"ctx {item['context']}%"
                )
        else:
            print("- no mid-session cache invalidation detected; large writes look like normal warm-up")

    print("\nRecommendations:")
    for item in build_recommendations(analysis)[:limit]:
        print(f"- {item}")


def build_recommendations(analysis: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    if analysis["repeated_reads"]:
        recommendations.append("Replace repeated full file reads with targeted line ranges or a short working summary.")
    if analysis["duplicate_waste"]:
        recommendations.append("Avoid re-running commands that return identical large output; summarize once and refer back.")
    if analysis["large_results"]:
        recommendations.append("Cap noisy command output with targeted filters, test failure excerpts, or artifact files.")
    if analysis["agent_reports"]:
        recommendations.append("Ask subagents for compact verdicts and paths first; fetch full reports only when needed.")
    if analysis["cache_invalidations"]:
        recommendations.append("Keep stable instructions/tool lists early and move volatile logs/results later in the turn.")
    if not recommendations:
        recommendations.append("No obvious context waste pattern found in this transcript.")
    return recommendations


def build_payload(transcript: Path, analysis: dict[str, Any], limit: int) -> dict[str, Any]:
    """Assemble a machine-readable summary for --json consumers."""
    status = analysis["status"] or {}
    total_chars = analysis["total_chars"]
    tool_result_total = sum(analysis["tool_result_chars"].values())

    def cat_rows(counter: Counter[str], whole: int) -> list[dict[str, Any]]:
        return [
            {"name": name, "est_tokens": est_tokens(chars), "share": round(chars / whole, 4) if whole else 0.0}
            for name, chars in counter.most_common(limit)
        ]

    attachments = {n: c for n, c in analysis["category_chars"].items() if n.startswith("attachment")}

    return {
        "schema": "claude-context-audit/1",
        "disclaimer": (
            "Heuristic, token-level estimates. Char counts are converted to tokens at ~4 chars/token. "
            "Cache classification uses per-turn deltas, not exact cache-block attribution."
        ),
        "transcript": str(transcript),
        "repo": status.get("repo"),
        "session_id": status.get("session_id"),
        "transcript_payload_est_tokens": est_tokens(total_chars),
        "transcript_payload_chars": total_chars,
        "session_totals": {
            "context_pct": status.get("context_pct") if status else None,
            "total_tokens": status.get("total_tokens") if status else None,
            "cache_read_tokens": status.get("cache_read_tokens") if status else None,
            "cache_write_tokens": status.get("cache_write_tokens") if status else None,
            "cost_usd": status.get("cost_usd") if status else None,
        } if status else None,
        "categories": cat_rows(analysis["category_chars"], total_chars),
        "attachments": {
            "total_est_tokens": est_tokens(sum(attachments.values())),
            "by_type": [
                {"name": n, "est_tokens": est_tokens(c)}
                for n, c in sorted(attachments.items(), key=lambda kv: kv[1], reverse=True)[:limit]
            ],
        },
        "tool_result_weight": cat_rows(analysis["tool_result_chars"], tool_result_total),
        "repeated_reads": [
            {"count": count, "result_est_tokens": est_tokens(chars), "label": path}
            for count, chars, path in analysis["repeated_reads"][:limit]
        ],
        "repeated_commands": [
            {"count": count, "result_est_tokens": est_tokens(chars), "label": cmd}
            for count, chars, cmd in analysis["repeated_commands"][:limit]
        ],
        "duplicate_blobs": [
            {
                "count": item["count"],
                "tool": item["tool"],
                "label": shorten(item["label"], 96),
                "repeated_est_tokens": est_tokens(item["size"] * (item["count"] - 1)),
            }
            for item in analysis["duplicate_waste"][:limit]
        ],
        "large_results": [
            {"tool": tool, "label": shorten(label, 96), "est_tokens": est_tokens(size)}
            for size, tool, label in analysis["large_results"][:limit]
        ],
        "agent_reports": [
            {"label": shorten(label, 96), "est_tokens": est_tokens(size)}
            for size, label in analysis["agent_reports"][:limit]
        ],
        "cache": {
            "has_step_data": bool(analysis["steps"]),
            "step_count": len(analysis["steps"]),
            "totals": dict(analysis["step_totals"]) if analysis["steps"] else None,
            "cache_read_share": analysis["cache_read_share"],
            "warmups": analysis["cache_warmups"][:limit],
            "invalidations": analysis["cache_invalidations"][:limit],
        },
        "recommendations": build_recommendations(analysis),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctx audit", description="Audit Claude Code transcript context/cache usage")
    parser.add_argument("target", nargs="?", help="transcript JSONL, status JSON, session id, or omitted for newest active session")
    parser.add_argument("--latest", action="store_true", help="audit the newest active session (the default when no target is given)")
    parser.add_argument("--session", help="audit by session id under --state-dir")
    parser.add_argument("--transcript", help="audit a specific transcript JSONL path")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="directory containing session-status JSON files")
    parser.add_argument("--limit", type=int, default=8, help="number of rows per section")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the text report")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).expanduser()

    # Selection precedence: --transcript > --session > positional target.
    # --latest (or nothing) resolves to the newest active session.
    selector = args.transcript or args.session or args.target
    if args.latest:
        selector = None

    transcript, status = resolve_target(selector, state_dir)
    if not transcript.exists():
        raise SystemExit(f"transcript does not exist: {transcript}")

    rows = read_jsonl(transcript)
    steps = load_steps(status, state_dir)
    analysis = analyze(rows, status, steps)
    limit = max(1, args.limit)

    if args.json:
        print(json.dumps(build_payload(transcript, analysis, limit), indent=2, sort_keys=True))
    else:
        print_report(transcript, analysis, limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
