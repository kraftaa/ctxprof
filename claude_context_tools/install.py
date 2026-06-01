"""`claude-ctx install` — wire the heartbeat statusline without clobbering one.

Claude Code allows exactly one statusLine. This never silently replaces an
existing one: if a different statusLine is configured it refuses and prints the
JSON to add by hand. --force replaces it (a timestamped backup is written).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

HEARTBEAT_RESOURCE = "claude-statusline-heartbeat.sh"


def heartbeat_path() -> Path:
    """Absolute on-disk path to the packaged heartbeat statusline script."""
    res = resources.files("claude_context_tools").joinpath("data", HEARTBEAT_RESOURCE)
    return Path(str(res))


def default_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS", "~/.claude/settings.json")).expanduser()


def wire_statusline(settings_path: Path, force: bool) -> int:
    heartbeat = heartbeat_path()
    if not heartbeat.exists():
        print(f"ERROR: packaged heartbeat not found at {heartbeat}", file=sys.stderr)
        return 1
    try:
        heartbeat.chmod(heartbeat.stat().st_mode | 0o111)
    except OSError:
        pass

    desired = {"type": "command", "command": f"bash {heartbeat}"}

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {settings_path} is not valid JSON ({exc}); fix it first.", file=sys.stderr)
            return 1

    existing = settings.get("statusLine")
    if existing == desired:
        print("statusLine already points at the heartbeat; nothing to do.")
        return 0
    if existing and not force:
        current = existing.get("command") if isinstance(existing, dict) else existing
        print("REFUSING to overwrite your existing statusLine. Claude Code allows only one.")
        print(f"  current: {json.dumps(current)}")
        print("  Re-run with --force (a backup is made) or set this by hand:")
        print(f'  "statusLine": {json.dumps(desired)}')
        return 0

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.with_suffix(settings_path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(settings_path, backup)
        print(f"backed up existing settings to {backup}")

    settings["statusLine"] = desired
    # Atomic write so a crash mid-write can't truncate settings.json.
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    tmp.replace(settings_path)
    print(f"statusLine set to the heartbeat in {settings_path}.")
    print("Restart Claude Code to pick it up.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ctx install", description="Wire the heartbeat statusline into Claude Code settings")
    parser.add_argument("--statusline", action="store_true", help="wire the heartbeat into settings.json (safe; refuses to clobber)")
    parser.add_argument("--force", action="store_true", help="replace an existing statusLine (writes a backup first)")
    parser.add_argument("--settings", default=None, help="path to settings.json (default ~/.claude/settings.json)")
    parser.add_argument("--print-path", action="store_true", help="print the packaged heartbeat script path and exit")
    args = parser.parse_args(argv)

    if args.print_path:
        print(heartbeat_path())
        return 0

    settings_path = Path(args.settings).expanduser() if args.settings else default_settings_path()
    if args.statusline:
        return wire_statusline(settings_path, args.force)

    # No action requested: show what's available.
    print("Heartbeat statusline script:")
    print(f"  {heartbeat_path()}")
    print("\nWire it into Claude Code (safe — refuses to clobber an existing statusLine):")
    print("  ctx install --statusline")
    print("\nOr add this to your settings.json by hand:")
    print(json.dumps({"statusLine": {"type": "command", "command": f"bash {heartbeat_path()}"}}, indent=2))
    return 0
