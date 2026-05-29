# claude-context-tools

Two small, dependency-free tools for understanding where a Claude Code session's
context and cache budget actually goes:

- **Dashboard** — a live "how much" monitor across all your open sessions.
- **Audit** — an offline "why did context/cache burn?" analyzer for one session.

Both read the same per-session **heartbeat** files that a statusline writes. The
statusline is the only piece that runs inside Claude Code; the dashboard and
audit are ordinary commands you run in any terminal.

```text
Claude Code session
        │  (each statusline refresh)
        ▼
 heartbeat statusline  ──writes──►  ~/.claude/session-status/<session>.json
                                    ~/.claude/session-status/steps/<session>.jsonl
        ▲                                    │
        │ one stdout line                    ├──► ctx dashboard  (live, all sessions)
   in-app status bar                         └──► ctx audit      (offline, one session)
```

Requires `python3` (3.9+). The heartbeat statusline also needs `jq` + `awk`
(standard on macOS/Linux).

## Install

```bash
# Recommended: isolated global install with pipx
pipx install claude-context-tools          # from PyPI (once published)
# or from a git checkout (replace <your-repo-url>):
pipx install "git+<your-repo-url>#subdirectory=claude-context-tools"

# Or plain pip (ideally in a venv)
pip install claude-context-tools

# Or straight from a clone, editable
pip install -e claude-context-tools
```

This installs one command, `ctx`, an umbrella with subcommands:
`dashboard`, `show`, `audit`, `statusline`, `install`. Run `ctx` with no
arguments to see them all.

No clone needed after install. To run without installing, use
`python3 -m claude_context_tools.cli <subcommand>` from this directory.

## Use it

```bash
# Live table of all active sessions (Ctrl-C to quit):
ctx dashboard

# Print once and exit (good for tmux panes / scripts):
ctx dashboard --refresh 0

# Include sessions idle past the stale timeout (shown dimmed, with `!`):
ctx dashboard --include-stale

# Details + a ready-to-run audit command for one session (or the newest):
ctx show
ctx show <session-id-or-prefix>

# Offline "why did it burn?" audit:
ctx audit --latest
ctx audit --session <session-id>
ctx audit --transcript ~/.claude/projects/<proj>/<session>.jsonl
ctx audit --latest --json        # machine-readable
```

## Turn on the heartbeat (one-time)

The dashboard and audit need a statusline that writes heartbeat files. Claude
Code supports **exactly one** `statusLine`, so this never silently replaces an
existing one:

```bash
ctx install --statusline          # safe: refuses if a statusLine exists
ctx install --statusline --force  # replace it (writes a timestamped backup)
ctx install --print-path          # just print the heartbeat script path
```

If you already have a statusline you like, keep it — just make it also write the
`~/.claude/session-status/<session>.json` and `steps/<session>.jsonl` shapes (see
`claude_context_tools/data/claude-statusline-heartbeat.sh` for the exact fields)
and these tools will read it. To wire it by hand instead:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /ABSOLUTE/PATH/TO/claude-statusline-heartbeat.sh"
  }
}
```

### Global vs per-repo

- **Global** (all sessions): set the heartbeat in `~/.claude/settings.json`.
  This is what `ctx install --statusline` does. Usual choice.
- **Per-repo**: set `statusLine` in a project's `.claude/settings.json`; it wins
  for sessions in that repo. Use an absolute path to the heartbeat script.

## What the audit reports

- **What burned context** — per-category token estimates (assistant/user text,
  thinking, tool inputs, per-tool results, attachments).
- **Attachment breakdown** — system-injected context (skill listings, hook
  output, deferred tool lists) totalled separately.
- **Repeated file reads / commands / duplicate output blobs** — content paid for
  more than once.
- **Large pasted logs / large tool results** and **subagent report weight**.
- **Cache telemetry** — per-turn behavior, separating expected initial
  **warm-up** from likely mid-session **invalidation** (cache was warm, then
  largely rewritten).
- **Recommendations** derived from the findings.

### Honest limits

- Token counts are **estimates** (`~4 chars/token` for transcript content);
  session-level numbers come from Claude Code's own usage fields.
- Cache analysis is **heuristic and token-level** — the heartbeat exposes per-turn
  token *counts*, not cache-block boundaries, so it flags *likely* invalidation,
  not exact cache-block attribution.
- **Unknown is not zero.** With no step file, the audit says per-turn cache
  behavior is unknown rather than reporting `0`.

## Dashboard notes

- **CONTEXT** is a fill bar + percent; **RECENT** is a per-session sparkline of
  the last few turns' token volume, colored by cache share (green = cached,
  cyan = fresh) — the same signal as the heartbeat statusline's bar.
- **Stale sessions** are hidden by default; the header shows `stale-hidden:N` so
  you know they exist. `--include-stale` shows them dimmed with an `!`. The
  cutoff is `CLAUDE_STATUS_STALE_AFTER_SECONDS` (default 900s). An open session
  that's idle (not generating turns) stops writing heartbeats and goes stale.
- **COST and TIME are cumulative session totals**; **live-ctx / IN / OUT** are
  the current context, not lifetime. An old session legitimately shows a large
  frozen cost/time — its lifetime total at the last heartbeat, not ongoing spend.

## Files written

| Path | Written by | Used by |
|---|---|---|
| `~/.claude/session-status/<session>.json` | heartbeat statusline | dashboard, audit |
| `~/.claude/session-status/steps/<session>.jsonl` | heartbeat statusline | audit (cache telemetry) |
| Claude transcript JSONL (under `~/.claude/projects/`) | Claude Code | audit |

Override the state dir with `CLAUDE_STATUS_STATE_DIR`. Nothing is deleted
automatically. These tools only ever **read** your transcripts — they never
modify or upload them.

## Tests

Fixture-based smoke tests, stdlib only (synthetic data, generated by
`tests/make_fixtures.py` — no private content):

```bash
python3 tests/test_claude_context_tools.py
python3 tests/make_fixtures.py    # regenerate fixtures after changing shapes
```

See [`../03-harnesses.md`](../03-harnesses.md) → "Status line" for how this fits
the broader harness guidance.
