#!/usr/bin/env python3
"""MCP risk surface for `ctx guard --mcp` and the `ctx attack-path` synthesis.

What can and can't be known, honestly:

- **Configured servers** come from `~/.claude.json` (`mcpServers` + per-project
  `projects.<path>.mcpServers`) and a project `.mcp.json`. These give a server's
  name and launch command — NOT its per-tool permissions.
- **Observed tools** come from the transcript: every `mcp__<server>__<tool>` call
  actually made. This is measured fact.
- **Capabilities/risk** are therefore reported on two bases, always labelled:
  `[catalog]` (what a *known* server can do — an assumption from a built-in list)
  and `[observed]` (classified from the tools actually called). A server that is
  neither in the catalog nor used reports risk `unknown`, never a guessed score.

`attack-path` chains the signals the other guard lenses already detect (injection,
untrusted input, capable MCP, shell use, secrets in context) into a *potential*
reachability path — it is a surface map, not proof of an exploit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from .audit import DEFAULT_STATE_DIR, read_jsonl, resolve_target

RISK_RANK = {"unknown": 0, "LOW": 1, "MED": 2, "HIGH": 3}


def worst(a: str, b: str) -> str:
    return a if RISK_RANK.get(a, 0) >= RISK_RANK.get(b, 0) else b


# Capability tag -> risk it implies.
TAG_RISK = {
    "exec": "HIGH", "cloud": "HIGH", "fs-write": "HIGH", "db-write": "HIGH",
    "network": "MED", "browser": "MED", "saas-write": "MED", "db": "MED", "fs-read": "MED",
    "saas-read": "LOW", "local": "LOW", "read": "LOW",
}

# Curated catalog of well-known MCP servers: (name/command keywords, tags, note).
# Matched as substrings against the server name + launch command + args.
CATALOG: list[tuple[tuple[str, ...], list[str], str]] = [
    (("filesystem", "file-system"), ["fs-read", "fs-write"], "Reads/writes local files"),
    (("github",), ["network", "saas-write"], "GitHub repos / PRs / issues"),
    (("gitlab",), ["network", "saas-write"], "GitLab repos / MRs"),
    (("slack",), ["network", "saas-write"], "Post/read Slack messages"),
    (("postgres", "postgresql", "mysql", "sqlite", "mongodb", "database", "db-mcp"), ["db"], "Database access"),
    (("playwright", "puppeteer", "browser", "chrome"), ["browser", "network"], "Controls a web browser"),
    (("fetch", "brave-search", "tavily", "web-search", "websearch"), ["network"], "Fetches web content"),
    (("memory", "sequential-thinking"), ["local"], "Local scratch / memory"),
    (("gdrive", "google-drive", "googledrive", "onedrive", "dropbox"), ["network", "fs-read"], "Cloud file storage"),
    (("aws", "gcp", "google-cloud", "azure", "kubernetes", "kubectl", "terraform"), ["cloud"], "Cloud / infra control"),
    (("shell", "terminal", "iterm", "command-runner", "exec"), ["exec"], "Executes shell commands"),
    (("notion", "linear", "jira", "confluence", "asana", "trello"), ["network", "saas-write"], "SaaS project tools"),
    (("sentry", "datadog", "grafana", "prometheus"), ["network", "saas-read"], "Observability (read)"),
    (("stripe", "paypal", "square"), ["network", "saas-write"], "Payments"),
    (("netsuite", "salesforce", "sap", "hubspot"), ["network", "saas-write"], "ERP / CRM"),
]

_MCP_TOOL = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__(.+)$")
_WRITE_HINTS = ("write", "create", "update", "delete", "remove", "put", "post", "insert",
                "upsert", "edit", "push", "merge", "send", "exec", "run", "eval", "spawn", "drop")
_NET_HINTS = ("navigate", "fetch", "http", "url", "request", "browse", "screenshot", "evaluate", "upload", "download")


def classify_tool(tool: str) -> str:
    t = tool.lower()
    if any(h in t for h in _WRITE_HINTS):
        return "write/exec"
    if any(h in t for h in _NET_HINTS):
        return "network"
    return "read"


def discover_servers(cwd: str | None) -> dict[str, dict[str, Any]]:
    """Configured MCP servers from ~/.claude.json (user + project scope) and .mcp.json."""
    servers: dict[str, dict[str, Any]] = {}

    def add(name: str, cfg: Any, scope: str, source: str) -> None:
        if name in servers or not isinstance(cfg, dict):
            return
        servers[name] = {
            "name": name, "scope": scope, "source": source,
            "command": str(cfg.get("command") or cfg.get("type") or ""),
            "args": [str(a) for a in (cfg.get("args") or [])],
        }

    home = Path("~/.claude.json").expanduser()
    if home.exists():
        try:
            data = json.loads(home.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for name, cfg in (data.get("mcpServers") or {}).items():
            add(name, cfg, "user", str(home))
        if cwd:
            proj = (data.get("projects") or {}).get(os.path.abspath(os.path.expanduser(cwd)))
            if isinstance(proj, dict):
                for name, cfg in (proj.get("mcpServers") or {}).items():
                    add(name, cfg, "project(local)", str(home))

    if cwd:
        mj = Path(cwd).expanduser() / ".mcp.json"
        if mj.exists():
            try:
                data = json.loads(mj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            for name, cfg in (data.get("mcpServers") or {}).items():
                add(name, cfg, "project", str(mj))

    return servers


def observed_mcp_tools(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    """server -> set of tool names actually called this session (from the transcript)."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                match = _MCP_TOOL.match(str(block.get("name") or ""))
                if match:
                    seen.setdefault(match.group(1), set()).add(match.group(2))
    return seen


def _catalog_match(server: dict[str, Any]) -> tuple[list[str], str]:
    name = server["name"].lower()
    blob = (name + " " + server.get("command", "") + " " + " ".join(server.get("args", []))).lower()
    for keys, tags, note in CATALOG:
        if any(k in name or k in blob for k in keys):
            return tags, note
    return [], ""


def _risk_from_tags(tags: list[str]) -> str:
    risk = "unknown"
    for tag in tags:
        risk = worst(risk, TAG_RISK.get(tag, "LOW"))
    return risk


def _risk_from_tools(tools: list[str]) -> str:
    risk = "unknown"
    for tool in tools:
        kind = classify_tool(tool)
        risk = worst(risk, "HIGH" if kind == "write/exec" else "MED" if kind == "network" else "LOW")
    return risk


def classify_servers(rows: list[dict[str, Any]], cwd: str | None) -> list[dict[str, Any]]:
    configured = discover_servers(cwd)
    observed = observed_mcp_tools(rows)
    out: list[dict[str, Any]] = []

    for name, server in configured.items():
        tags, note = _catalog_match(server)
        tools = sorted(observed.get(name, set()))
        risk = worst(_risk_from_tags(tags), _risk_from_tools(tools))
        basis = "+".join([b for b in ("catalog" if tags else "", "observed" if tools else "") if b]) or "unknown"
        out.append({**server, "tags": tags, "note": note, "observed_tools": tools, "risk": risk, "basis": basis})

    # Servers seen in the transcript but not in local config (remote/claude.ai connectors).
    for name, tools in observed.items():
        if name in configured:
            continue
        tags, note = _catalog_match({"name": name, "command": "", "args": []})
        tool_list = sorted(tools)
        risk = worst(_risk_from_tags(tags), _risk_from_tools(tool_list))
        out.append({
            "name": name, "scope": "remote/connector (not in local config)", "source": "transcript",
            "command": "", "args": [], "tags": tags, "note": note,
            "observed_tools": tool_list, "risk": risk, "basis": "+".join([b for b in ("catalog" if tags else "", "observed") if b]),
        })

    out.sort(key=lambda s: (-RISK_RANK.get(s["risk"], 0), s["name"]))
    return out


def render_mcp(rows: list[dict[str, Any]], status: dict[str, Any] | None, as_json: bool) -> str:
    cwd = (status or {}).get("cwd") or (status or {}).get("project_dir")
    servers = classify_servers(rows, cwd)
    if as_json:
        return json.dumps({"schema": "claude-context-mcp/1", "servers": servers,
                           "disclaimer": "Capabilities tagged [catalog] are assumptions about known servers; "
                                         "[observed] are classified from actual tool calls. unknown = not in "
                                         "catalog and not used."}, indent=2, sort_keys=True)
    if not servers:
        return ("MCP Risk Report\n  No MCP servers configured (in ~/.claude.json / .mcp.json) "
                "or called in this session.")
    lines = ["MCP Risk Report",
             "  [catalog] = what a known server can do (assumption); [observed] = from actual tool calls.", ""]
    for s in servers:
        lines.append(f"  {s['name']}  ({s['scope']})  RISK: {s['risk']}  [{s['basis']}]")
        if s["note"]:
            lines.append(f"      catalog: {s['note']}  ({', '.join(s['tags'])})")
        if s["observed_tools"]:
            kinds = sorted({classify_tool(t) for t in s["observed_tools"]})
            lines.append(f"      observed: {len(s['observed_tools'])} tool(s) used [{', '.join(kinds)}] "
                         f"e.g. {', '.join(s['observed_tools'][:4])}")
        if not s["note"] and not s["observed_tools"]:
            lines.append("      capability: unknown — not in catalog, not used this session")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_attack_path(rows: list[dict[str, Any]], status: dict[str, Any] | None) -> dict[str, Any]:
    """Chain the guard lenses into a potential injection->action->exfil reachability path."""
    from . import guard  # local import: guard imports nothing from here, keep it lazy anyway

    cwd = (status or {}).get("cwd") or (status or {}).get("project_dir")
    servers = classify_servers(rows, cwd)

    injection = guard.scan_injection(rows)
    base = guard.scan(rows)
    secrets = [f for f in base if f["kind"] in ("secret", "secret-file")]
    dangerous = [f for f in base if f["kind"] == "command"]

    web_calls = 0
    shell_calls = 0
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                if name in guard.WEB_TOOLS:
                    web_calls += 1
                elif name in guard.SINK_SHELL:
                    shell_calls += 1

    capable = [s for s in servers if any(t in ("fs-write", "exec", "cloud", "network", "saas-write", "db")
                                         for t in s["tags"]) or any(classify_tool(t) != "read" for t in s["observed_tools"])]

    links = [
        {"node": "Prompt injection in ingested content", "present": bool(injection),
         "detail": f"{len(injection)} phrase(s)"},
        {"node": "Untrusted external input (WebFetch/WebSearch)", "present": web_calls > 0,
         "detail": f"{web_calls} call(s)"},
        {"node": "Capable MCP available (write/network/exec)", "present": bool(capable),
         "detail": ", ".join(s["name"] for s in capable[:3]) or "none"},
        {"node": "Shell execution used", "present": shell_calls > 0 or bool(dangerous),
         "detail": f"{shell_calls} Bash call(s), {len(dangerous)} dangerous"},
        {"node": "Credentials present in context", "present": bool(secrets),
         "detail": f"{len(secrets)} secret(s)"},
    ]

    entry = links[0]["present"] or links[1]["present"]    # an untrusted entry point exists
    means = links[2]["present"] or links[3]["present"]    # a capable actuator exists
    target = links[4]["present"]                          # something worth reaching
    if entry and means and target:
        overall = "HIGH"
    elif sum(1 for x in links if x["present"]) >= 3:
        overall = "MED"
    elif any(x["present"] for x in links):
        overall = "LOW"
    else:
        overall = "none"
    return {"links": links, "overall": overall}


def render_attack_path(rows: list[dict[str, Any]], status: dict[str, Any] | None, as_json: bool) -> str:
    result = build_attack_path(rows, status)
    if as_json:
        return json.dumps({"schema": "claude-context-attack-path/1", **result,
                           "disclaimer": "A potential reachability surface chained from detected signals — "
                                         "NOT proof of an exploit. Each node is a real detection."}, indent=2, sort_keys=True)
    lines = ["Attack-surface path  (potential reachability, not an observed exploit)", ""]
    for i, link in enumerate(result["links"]):
        mark = "✓" if link["present"] else " "
        lines.append(f"  [{mark}] {link['node']}  —  {link['detail']}")
        if i < len(result["links"]) - 1:
            lines.append("        ↓")
    lines.append("")
    explain = {
        "HIGH": "an untrusted entry, a capable actuator, and credentials are all present — "
                "a prompt injection could plausibly chain to credential exposure",
        "MED": "several links present; review the chain",
        "LOW": "only isolated links present",
        "none": "no links detected",
    }[result["overall"]]
    lines.append(f"OVERALL RISK: {result['overall']} — {explain}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctx attack-path", description="Potential injection->action->exfil reachability for a session")
    parser.add_argument("target", nargs="?", help="transcript JSONL, status JSON, session id, or omitted for newest active session")
    parser.add_argument("--latest", action="store_true", help="use the newest active session")
    parser.add_argument("--session", help="session id under --state-dir")
    parser.add_argument("--transcript", help="a specific transcript JSONL path")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="session-status directory")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).expanduser()
    selector = args.transcript or args.session or args.target
    if args.latest:
        selector = None
    transcript, status = resolve_target(selector, state_dir)
    if not transcript.exists():
        raise SystemExit(f"transcript does not exist: {transcript}")
    print(render_attack_path(read_jsonl(transcript), status, args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
