"""Model pricing table ($/MTok) for cost *modeling*.

The COST shown in `ctx steps`/`digest`/dashboard comes from Claude Code's own
`total_cost_usd` (authoritative — it already uses the real model + current
price). This table is only for forward-looking what-if math: keep-warm cost,
rebuild cost, and the keep-alive break-even.

Update prices here, or override without editing code via a JSON file at
~/.claude/ctx-pricing.json (or $CLAUDE_CTX_PRICING), e.g.:

    { "opus-4.9": {"input": 5, "output": 25, "cache_read": 0.5,
                   "cache_write_5m": 6.25, "cache_write_1h": 10} }

Source: https://platform.claude.com/docs/en/about-claude/pricing  (verified 2026-06)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# $/MTok. Keys are "<family>-<version>".
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    # Opus 4.5–4.8 share one tier
    "opus-4.8": {"input": 5, "output": 25, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10},
    "opus-4.7": {"input": 5, "output": 25, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10},
    "opus-4.6": {"input": 5, "output": 25, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10},
    "opus-4.5": {"input": 5, "output": 25, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10},
    # Opus 4 / 4.1 (deprecated, old pricing)
    "opus-4.1": {"input": 15, "output": 75, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30},
    "opus-4": {"input": 15, "output": 75, "cache_read": 1.50, "cache_write_5m": 18.75, "cache_write_1h": 30},
    "sonnet-4.6": {"input": 3, "output": 15, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6},
    "sonnet-4.5": {"input": 3, "output": 15, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6},
    "sonnet-4": {"input": 3, "output": 15, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6},
    "haiku-4.5": {"input": 1, "output": 5, "cache_read": 0.10, "cache_write_5m": 1.25, "cache_write_1h": 2},
}
DEFAULT_MODEL = "opus-4.8"
OVERRIDE_PATH = Path(os.environ.get("CLAUDE_CTX_PRICING", "~/.claude/ctx-pricing.json")).expanduser()


def load_pricing() -> dict[str, dict[str, float]]:
    """Default table, merged with the user's override file if present."""
    pricing = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    if OVERRIDE_PATH.exists():
        try:
            user = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
            for key, rates in user.items():
                if isinstance(rates, dict):
                    pricing[key] = {**pricing.get(key, {}), **rates}
        except (OSError, json.JSONDecodeError):
            pass
    return pricing


def match_model(name: str | None, pricing: dict[str, Any] | None = None) -> str:
    """Map a heartbeat model string ('Opus 4.8 (1M context)') to a pricing key."""
    pricing = pricing or load_pricing()
    s = (name or "").lower()
    family = next((f for f in ("opus", "sonnet", "haiku") if f in s), "")
    match = re.search(r"(\d+\.\d+|\d+)", s)
    version = match.group(1) if match else ""
    key = f"{family}-{version}"
    if key in pricing:
        return key

    # fall back to the highest known version in the same family (numeric sort so
    # 4.10 > 4.9), else the default.
    def _ver(k: str) -> list[int]:
        return [int(p) for p in re.findall(r"\d+", k.split("-", 1)[-1])] or [0]

    fam_keys = sorted((k for k in pricing if k.startswith(family + "-")), key=_ver, reverse=True)
    return fam_keys[0] if fam_keys else DEFAULT_MODEL


def cache_economics(
    context_tokens: float,
    model_key: str,
    pricing: dict[str, Any] | None = None,
    ping_interval_s: float = 240,
    cache: str = "5m",
) -> dict[str, Any]:
    """Keep-warm vs rebuild economics for a context of `context_tokens`."""
    pricing = pricing or load_pricing()
    rates = pricing.get(model_key, pricing.get(DEFAULT_MODEL, {}))
    read = rates.get("cache_read", 0.0)
    write = rates.get(f"cache_write_{cache}", rates.get("cache_write_5m", 0.0))
    ping = context_tokens * read / 1_000_000
    pings_per_hour = 3600 / ping_interval_s if ping_interval_s else 0
    warm_per_hour = pings_per_hour * ping
    rebuild = context_tokens * write / 1_000_000
    # None = keeping warm is free (no pings), so there's no break-even point.
    breakeven_h = rebuild / warm_per_hour if warm_per_hour else None
    return {
        "model_key": model_key,
        "context_tokens": context_tokens,
        "cache": cache,
        "read_rate": read,
        "write_rate": write,
        "ping_cost": ping,
        "pings_per_hour": pings_per_hour,
        "warm_per_hour": warm_per_hour,
        "rebuild_cost": rebuild,
        "breakeven_hours": breakeven_h,
    }
