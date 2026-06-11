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
        r"\s*[:=]\s*['\"]?([^\s'\"]{12,})"
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

# Dangerous shell commands: (label, regex, severity, hint).
DANGEROUS_CMDS: list[tuple[str, re.Pattern[str], str, str]] = [
    ("pipe remote script to shell", re.compile(r"(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b"), "HIGH",
     "Downloads and executes a remote script unverified."),
    ("recursive force remove of root/home", re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+(?:--\s+)?(?:/|~|\$HOME|\*)(?:\s|/|$)"), "HIGH",
     "Recursive force-delete targeting / or home."),
    ("recursive force remove", re.compile(r"\brm\s+-[a-z]*r[a-z]*f"), "MED",
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


def redact(secret: str) -> str:
    """Mask a secret so the report can't leak it: prefix + length + fingerprint."""
    s = secret.strip()
    head = s[:3]
    return f"{head}… (len {len(s)}, sha1:{sha(s)[:8]})"


def redact_secrets_in(text: str) -> str:
    """Replace any embedded secret in free text (e.g. a command) with its redaction."""
    out = text
    for _label, rx, _sev in SECRET_PATTERNS:
        out = rx.sub(lambda m: redact(m.group(1) if m.groups() else m.group(0)), out)
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

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["kind"], f["title"]))
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
            "fingerprint. Scans context that entered this session (Tier 1: known-format secrets, "
            "sensitive file reads, dangerous shell commands)."
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
    findings = scan(rows)
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
