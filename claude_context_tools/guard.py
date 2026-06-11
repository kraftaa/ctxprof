#!/usr/bin/env python3
"""Scan a Claude Code transcript for security risks (`ctx guard`).

Tier 1: secrets that entered the model's context, and dangerous shell commands
that ran during the session. This is a *session trust-boundary* scanner — not a
repo secret scanner. It answers "what sensitive material actually crossed into
this conversation, and what risky actions were taken," using the same transcript
`ctx audit` reads.

Safety: findings never print raw secret values. A match is redacted to a short
prefix + length + sha1 fingerprint, so the report itself cannot leak the secret
it is warning about.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .audit import (
    DEFAULT_STATE_DIR,
    content_text,
    read_jsonl,
    resolve_target,
    sha,
    shorten,
)

SEVERITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}

# Strong-format secret patterns. The whole match is the secret unless the regex
# captures it in group 1 (so the surrounding keyword isn't treated as secret).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "HIGH"),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "HIGH"),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), "HIGH"),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "HIGH"),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "HIGH"),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "HIGH"),
    ("Stripe secret key", re.compile(r"\bsk_live_[0-9A-Za-z]{24,}\b"), "HIGH"),
    ("Bearer token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-]{20,})"), "MED"),
    ("generic secret assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|password|passwd|client[_-]?secret)\b"
        r"\s*[:=]\s*['\"]?([^\s'\";&|<>(){}\[\]]{12,})"
    ), "MED"),
]

# Values that look like placeholders rather than real secrets — suppressed.
PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|<[^>]*>|\$\{?[a-z_]|your[_-]?|example|changeme|change_me|"
    r"redacted|placeholder|dummy|sample|test(?:ing)?|none|null|true|false|insert|todo)"
)

# Sensitive files: matched against the full path and the basename. The path is
# not itself a secret, so it is shown verbatim.
SENSITIVE_FILES: list[tuple[str, re.Pattern[str], str]] = [
    ("dotenv file", re.compile(r"(?:^|/)\.env(?:\.[\w.-]+)?$"), "HIGH"),
    ("PEM / private key file", re.compile(r"\.(?:pem|key|p12|pfx|keystore|jks)$", re.I), "HIGH"),
    ("SSH private key", re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"), "HIGH"),
    ("cloud credentials file", re.compile(r"(?:^|/)(?:credentials|\.netrc|\.npmrc|\.pypirc)$"), "HIGH"),
    ("kube/service-account config", re.compile(r"(?:^|/)(?:kubeconfig|service-account[\w.-]*\.json)$", re.I), "MED"),
    ("secrets file", re.compile(r"(?:^|/)secrets?(?:\.[\w.-]+)?$", re.I), "MED"),
]

# Match `rm` recursive+force flags in any order/case: -rf, -fr, -Rf, -rfv, etc.
_RM_FLAGS = r"-[a-zA-Z]*(?:[rR][a-zA-Z]*[fF]|[fF][a-zA-Z]*[rR])[a-zA-Z]*"

# Dangerous shell commands: (label, regex, severity, hint).
DANGEROUS_CMDS: list[tuple[str, re.Pattern[str], str, str]] = [
    ("pipe remote script to shell", re.compile(r"(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:bash|zsh|dash|ash|sh)\b"), "HIGH",
     "Downloads and executes a remote script unverified."),
    ("recursive force remove of root/home", re.compile(r"\brm\s+" + _RM_FLAGS + r"\s+(?:--\s+)?(?:/|~|\$HOME|\*)(?:\s|/|$)"), "HIGH",
     "Recursive force-delete targeting / or home."),
    ("recursive force remove", re.compile(r"\brm\s+" + _RM_FLAGS), "MED",
     "Recursive force-delete; verify the target path."),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "HIGH",
     "Classic fork bomb."),
    ("world-writable chmod", re.compile(r"\bchmod\s+(?:-[a-zR]+\s+)?0?777\b"), "MED",
     "Grants world write/execute permissions."),
    ("write to block device", re.compile(r"\b(?:dd\s+if=|mkfs(?:\.\w+)?\s|>\s*/dev/(?:sd|nvme|disk))"), "HIGH",
     "Writes directly to a block device."),
    ("sudo invocation", re.compile(r"(?:^|[;&|]|\s)sudo\s+\S"), "LOW",
     "Runs with elevated privileges."),
]

# Commands whose arguments are pure data/text, never executed — a dangerous
# substring inside them (e.g. `echo "curl x | bash"`, `grep -r 'rm -rf'`) is a
# quotation, not an action. Suppress dangerous-command findings for these to cut
# the most common false positive. Interpreters (python -c, node -e) are NOT here:
# their args can genuinely be malicious code, so we keep flagging them.
CARRIER_CMDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack"}

# Taint: untrusted content entered context, then a risky action ran soon after.
TAINT_WINDOW = 3  # turns
WEB_TOOLS = {"WebFetch", "WebSearch"}
SINK_SHELL = {"Bash", "Shell"}
SINK_EDIT = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Prompt-injection / jailbreak phrases. Scanned only in *ingested* content (tool
# results — files read, pages fetched, command output), never the user's own
# messages or the assistant's text. High-signal imperative patterns only, all LOW
# severity: these legitimately appear in security docs and prompt-engineering
# content, so treat hits as leads, not proof. Source tools whose results are
# ingested external content:
INGEST_TOOLS = {"Read", "WebFetch", "WebSearch", "Bash", "Shell", "Grep", "Glob", "Task", "Agent"}
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore previous instructions", re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
        r"(?:previous|prior|above|earlier|preceding|initial|original)\s+"
        r"(?:instructions?|prompts?|messages?|directions?|context|rules?)")),
    ("override/leak system prompt", re.compile(
        r"(?i)\b(?:override|bypass|ignore|reveal|print|show|leak|repeat|exfiltrate|disclose)\s+"
        r"(?:your|the|my|all)?\s*(?:system\s+)?(?:prompt|instructions?|guidelines?|rules?)")),
    ("role reassignment", re.compile(
        r"(?i)\b(?:you\s+are\s+now|from\s+now\s+on,?\s+you|act\s+as|pretend\s+(?:to\s+be|you(?:'re|\s+are)))\b")),
    ("hidden-from-user directive", re.compile(
        r"(?i)\bdo\s+not\s+(?:tell|inform|mention|reveal|notify|alert)\s+(?:the\s+|to\s+the\s+)?(?:user|operator|human|developer)")),
    ("model control tokens in data", re.compile(
        r"<\|im_start\|>|<\|im_end\|>|\[/?INST\]|<<SYS>>|</?system>")),
    ("jailbreak / developer mode", re.compile(
        r"(?i)\b(?:developer\s+mode|jailbreak|DAN\s+mode|do\s+anything\s+now)\b")),
]


def redact(secret: str) -> str:
    """Mask a secret so the report can't leak it: length + fingerprint only.

    No prefix is emitted — for format-prefixed keys (AKIA…, ghp_…) it would be
    constant and useless, and for generic password/token values it would leak the
    first real characters. The sha1 still lets identical secrets dedupe/correlate.
    """
    s = secret.strip()
    return f"«secret» (len {len(s)}, sha1:{sha(s)[:8]})"


def redact_secrets_in(text: str) -> str:
    """Replace any embedded secret in free text (e.g. a command) with its redaction.

    When a pattern captures only the value in group 1, only that span is masked, so
    the surrounding context (e.g. the `API_KEY=` keyword) is preserved for triage.
    """
    def _mask(match: re.Match[str]) -> str:
        if match.groups() and match.group(1) is not None:
            whole, start = match.group(0), match.start()
            g0, g1 = match.start(1) - start, match.end(1) - start
            return whole[:g0] + redact(match.group(1)) + whole[g1:]
        return redact(match.group(0))

    out = text
    for _label, rx, _sev in SECRET_PATTERNS:
        out = rx.sub(_mask, out)
    return out


def scan_text(text: str, where: str, findings: list[dict[str, Any]], seen: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not text:
        return
    for label, rx, sev in SECRET_PATTERNS:
        for match in rx.finditer(text):
            secret = (match.group(1) if match.groups() else match.group(0)) or ""
            secret = secret.strip()
            if not secret or PLACEHOLDER.match(secret):
                continue
            key = (label, sha(secret))
            if key in seen:
                seen[key]["count"] += 1
                continue
            finding = {
                "severity": sev,
                "kind": "secret",
                "title": f"{label} present in context",
                "evidence": redact(secret),
                "where": where,
                "count": 1,
            }
            seen[key] = finding
            findings.append(finding)


def check_sensitive_file(path: str, where: str, findings: list[dict[str, Any]]) -> None:
    if not path:
        return
    base = os.path.basename(path)
    for label, rx, sev in SENSITIVE_FILES:
        if rx.search(path) or rx.search(base):
            findings.append({
                "severity": sev,
                "kind": "secret-file",
                "title": f"{label} read into context",
                "evidence": path,
                "where": where,
                "count": 1,
            })
            return


def check_command(cmd: str, where: str, findings: list[dict[str, Any]]) -> None:
    if not cmd:
        return
    tokens = cmd.strip().split()
    if tokens and os.path.basename(tokens[0]) in CARRIER_CMDS:
        return  # args are data, not executed — a dangerous substring is a quotation
    preview = shorten(redact_secrets_in(cmd), 100)
    hits = [(label, sev, hint) for label, rx, sev, hint in DANGEROUS_CMDS if rx.search(cmd)]
    labels = {label for label, _sev, _hint in hits}
    for label, sev, hint in hits:
        # `rm -rf /` matches both the root-targeted HIGH and the generic MED;
        # keep only the more specific HIGH in that case.
        if label == "recursive force remove" and "recursive force remove of root/home" in labels:
            continue
        findings.append({
            "severity": sev,
            "kind": "command",
            "title": label,
            "evidence": preview,
            "hint": hint,
            "where": where,
            "count": 1,
        })


def _sort_key(finding: dict[str, Any]) -> tuple[int, str, str]:
    return (SEVERITY_ORDER.get(finding["severity"], 9), finding["kind"], finding["title"])


def _is_external(path: str, roots: list[str]) -> bool:
    """True if an absolute path falls outside every known repo/cwd root.

    Compares on path boundaries (root, or root + os.sep) so that e.g. ``/repo`` does
    not spuriously contain ``/repo-other`` or ``/repository``.
    """
    try:
        resolved = os.path.abspath(os.path.expanduser(path))
    except (OSError, ValueError):
        return False
    return not any(resolved == root or resolved.startswith(root + os.sep) for root in roots)


def scan_taint(rows: list[dict[str, Any]], roots: list[str]) -> list[dict[str, Any]]:
    """Flag *potential* taint: untrusted content (web fetch/search, or a file read
    outside the repo) followed within a few turns by a shell command or file edit.

    A heuristic and deliberately low/medium confidence — it cannot prove the action
    was influenced by the content, only that the content arrived first. Reads are
    only treated as untrusted when repo roots are known (otherwise we can't tell
    external from internal, and stay silent rather than guess).
    """
    findings: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    turn = 0
    for row in rows:
        turn += 1
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name in WEB_TOOLS:
                detail = inp.get("url") or inp.get("query") or ""
                pending = {"turn": turn, "source": name, "detail": shorten(str(detail), 60)}
            elif name == "Read" and roots:
                path = str(inp.get("file_path") or inp.get("path") or "")
                if path and _is_external(path, roots):
                    pending = {"turn": turn, "source": "read outside repo", "detail": shorten(path, 60)}
            elif pending and 0 <= turn - pending["turn"] <= TAINT_WINDOW:
                # delta 0 = source and sink in the same message (parallel tool calls)
                # — the tightest, highest-confidence fetch-then-execute pattern.
                if name in SINK_SHELL:
                    sev, act = "MED", "shell command"
                elif name in SINK_EDIT:
                    sev, act = "LOW", "file edit"
                else:
                    continue
                findings.append({
                    "severity": sev,
                    "kind": "taint",
                    "title": f"untrusted input then {act}",
                    "evidence": f"{pending['source']} ({pending['detail']}) → {name}",
                    "hint": "Untrusted content entered context just before this action — confirm the action wasn't steered by it.",
                    "where": f"turns {pending['turn']}→{turn}",
                    "count": 1,
                })
                pending = None
    return findings


def scan_injection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag prompt-injection / jailbreak phrases in *ingested* content only.

    Only tool results from content-bearing tools (file reads, web fetches, command
    output) are scanned — not the user's own messages or the assistant's text, since
    those aren't an injection surface. All findings are LOW and deduped by phrase, as
    these patterns occur in legitimate security/prompt-engineering material.
    """
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    tool_names: dict[str, str] = {}
    turn = 0
    for row in rows:
        turn += 1
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_names[str(block.get("id") or "")] = str(block.get("name") or "")
            elif block.get("type") == "tool_result":
                source = tool_names.get(str(block.get("tool_use_id") or ""), "")
                # Allowlist: only scan results we can positively attribute to an
                # ingest tool. An unresolvable id (e.g. tool_use in an earlier
                # transcript slice) is skipped rather than scanned by default.
                if source not in INGEST_TOOLS:
                    continue
                text = content_text(block.get("content"))
                if not text:
                    continue
                for label, rx in INJECTION_PATTERNS:
                    match = rx.search(text)
                    if not match:
                        continue
                    snippet = shorten(match.group(0), 80)
                    key = (label, sha(snippet.lower()))
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append({
                        "severity": "LOW",
                        "kind": "injection",
                        "title": f"possible prompt injection: {label}",
                        "evidence": f'"{snippet}"  (in {source or "tool"} result)',
                        "hint": "This phrase entered context from ingested content — verify it isn't steering the model.",
                        "where": f"turn {turn}",
                        "count": 1,
                    })
    return findings


def scan(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    turn = 0
    for row in rows:
        turn += 1
        where = f"turn {turn}"
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")

        if isinstance(content, str):
            scan_text(content, where, findings, seen)
            continue
        if not isinstance(content, list):
            attachment = row.get("attachment")
            if attachment:
                scan_text(content_text(attachment), where, findings, seen)
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                scan_text(content_text(block.get("text")), where, findings, seen)
            elif block_type == "thinking":
                scan_text(content_text(block.get("thinking") or block.get("text")), where, findings, seen)
            elif block_type == "tool_use":
                name = str(block.get("name") or "tool")
                input_data = block.get("input")
                if isinstance(input_data, dict):
                    if name == "Read":
                        check_sensitive_file(str(input_data.get("file_path") or input_data.get("path") or ""), where, findings)
                    elif name in {"Bash", "Shell"}:
                        check_command(str(input_data.get("command") or input_data.get("cmd") or ""), where, findings)
                # A secret pasted into any tool input (e.g. Write) still entered context.
                scan_text(content_text(input_data), where, findings, seen)
            elif block_type == "tool_result":
                scan_text(content_text(block.get("content")), where, findings, seen)

    findings.sort(key=_sort_key)
    return findings


def print_report(transcript: Path, status: dict[str, Any] | None, findings: list[dict[str, Any]], limit: int) -> None:
    repo = (status or {}).get("repo") or "-"
    session = (status or {}).get("session_id") or "-"
    print("Claude Context Guard (Tier 1 — secrets & dangerous commands)")
    print(f"Transcript: {transcript}")
    print(f"Repo: {repo}  Session: {session}")
    print("Heuristic, redacted — secret values are never shown, only a prefix + length + sha1 fingerprint.")

    if not findings:
        print("\nNo secret exposure or dangerous commands detected (Tier 1 patterns).")
        return

    counts = Counter(f["severity"] for f in findings)
    summary = "  ".join(f"{sev} {counts[sev]}" for sev in ("HIGH", "MED", "LOW") if counts.get(sev))
    print(f"\n{len(findings)} finding(s): {summary}")
    for finding in findings[:limit]:
        print(f"\n[{finding['severity']}] {finding['title']} ({finding['where']})")
        print(f"    {finding['evidence']}")
        if finding.get("hint"):
            print(f"    → {finding['hint']}")
        if finding.get("count", 1) > 1:
            print(f"    seen {finding['count']}x")
    if len(findings) > limit:
        print(f"\n... {len(findings) - limit} more (raise --limit to see all)")


def build_payload(transcript: Path, status: dict[str, Any] | None, findings: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    counts = Counter(f["severity"] for f in findings)
    return {
        "schema": "claude-context-guard/1",
        "disclaimer": (
            "Heuristic, redacted. Secret values are never emitted — only a prefix, length, and sha1 "
            "fingerprint. Scans context that entered this session: known-format secrets, sensitive file "
            "reads, dangerous shell commands, potential taint (untrusted input shortly before a "
            "shell/edit action), and prompt-injection phrases in ingested content — the last two are "
            "low confidence (correlation/heuristic, not proof)."
        ),
        "transcript": str(transcript),
        "repo": (status or {}).get("repo"),
        "session_id": (status or {}).get("session_id"),
        "counts": {sev: counts.get(sev, 0) for sev in ("HIGH", "MED", "LOW")},
        "findings": findings[:limit],
        "total_findings": len(findings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctx guard", description="Scan a Claude Code transcript for secrets & dangerous commands")
    parser.add_argument("target", nargs="?", help="transcript JSONL, status JSON, session id, or omitted for newest active session")
    parser.add_argument("--latest", action="store_true", help="scan the newest active session (the default when no target is given)")
    parser.add_argument("--session", help="scan by session id under --state-dir")
    parser.add_argument("--transcript", help="scan a specific transcript JSONL path")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="directory containing session-status JSON files")
    parser.add_argument("--limit", type=int, default=20, help="max findings to print")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the text report")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any finding is reported (for CI gating)")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).expanduser()
    selector = args.transcript or args.session or args.target
    if args.latest:
        selector = None

    transcript, status = resolve_target(selector, state_dir)
    if not transcript.exists():
        raise SystemExit(f"transcript does not exist: {transcript}")

    rows = read_jsonl(transcript)
    roots = [
        os.path.abspath(os.path.expanduser(str(status[key])))
        for key in ("project_dir", "cwd")
        if status and status.get(key)
    ]
    findings = scan(rows) + scan_taint(rows, roots) + scan_injection(rows)
    findings.sort(key=_sort_key)
    limit = max(1, args.limit)

    if args.json:
        print(json.dumps(build_payload(transcript, status, findings, limit), indent=2, sort_keys=True))
    else:
        print_report(transcript, status, findings, limit)

    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
