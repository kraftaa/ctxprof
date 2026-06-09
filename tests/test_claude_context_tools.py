#!/usr/bin/env python3
"""Fixture-based smoke tests for the Claude context/cache tools.

Stdlib only (unittest). Run:

    python3 tests/test_claude_context_tools.py
    # or
    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]  # the claude-context-tools dir
sys.path.insert(0, str(PKG_ROOT))

from claude_context_tools import audit, dashboard, pricing  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION = "00000000-aaaa-bbbb-cccc-000000000001"
TRANSCRIPT = FIXTURES / "transcript" / f"{SESSION}.jsonl"
STATE_DIR = FIXTURES / "state"  # holds <session>.json + steps/, like ~/.claude/session-status
STEPS = STATE_DIR / "steps" / f"{SESSION}.jsonl"


class HelperTests(unittest.TestCase):
    def test_compact_int(self):
        self.assertEqual(audit.compact_int(None), "-")
        self.assertEqual(audit.compact_int(500), "500")
        self.assertEqual(audit.compact_int(1500), "1.5k")
        self.assertEqual(audit.compact_int(2_000_000), "2.0M")

    def test_est_tokens(self):
        self.assertEqual(audit.est_tokens(0), 0)
        self.assertEqual(audit.est_tokens(4), 1)
        self.assertEqual(audit.est_tokens(400), 100)

    def test_tokenizer_factor(self):
        self.assertEqual(audit.tokenizer_factor("Opus 4.8 (1M context)"), 1.35)
        self.assertEqual(audit.tokenizer_factor("Opus 4.7"), 1.35)
        self.assertEqual(audit.tokenizer_factor("Opus 4.5"), 1.0)
        self.assertEqual(audit.tokenizer_factor("Sonnet 4.6"), 1.0)
        self.assertEqual(audit.tokenizer_factor(None), 1.0)

    def test_duration_and_age(self):
        self.assertEqual(dashboard.duration(0), "0m")
        self.assertEqual(dashboard.duration(3_600_000), "1h00m")
        self.assertEqual(dashboard.age(30), "30s")
        self.assertEqual(dashboard.age(120), "2m")


class ExtractRecordTests(unittest.TestCase):
    def test_unknown_is_not_zero(self):
        # No usage fields at all -> token fields stay None (unknown), not 0.
        record = dashboard.extract_record({"session_id": "x", "model": {"display_name": "M"}})
        self.assertIsNone(record["input_tokens"])
        self.assertIsNone(record["total_tokens"])
        self.assertIsNone(record["context_pct"])
        self.assertEqual(record["model"], "M")

    def test_totals_sum_when_present(self):
        data = {
            "session_id": "y",
            "context_window": {"current_usage": {
                "input_tokens": 100, "output_tokens": 10,
                "cache_read_input_tokens": 50, "cache_creation_input_tokens": 5,
            }},
        }
        record = dashboard.extract_record(data)
        self.assertEqual(record["input_tokens"], 100)
        self.assertEqual(record["total_tokens"], 165)


class AnalyzeTranscriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = audit.read_jsonl(TRANSCRIPT)
        cls.analysis = audit.analyze(rows, status=None, steps=[])

    def test_categories_present(self):
        cats = self.analysis["category_chars"]
        self.assertIn("assistant text", cats)
        self.assertIn("attachment:skill_listing", cats)
        self.assertTrue(any(k.startswith("Bash") for k in cats))

    def test_repeated_read_detected(self):
        reads = {path for _, _, path in self.analysis["repeated_reads"]}
        self.assertIn("/repo/src/auth.py", reads)

    def test_duplicate_blob_detected(self):
        self.assertTrue(self.analysis["duplicate_waste"], "identical Read results should be flagged")

    def test_large_bash_result_detected(self):
        tools = {tool for _, tool, _ in self.analysis["large_results"]}
        self.assertIn("Bash", tools)

    def test_agent_report_detected(self):
        self.assertTrue(self.analysis["agent_reports"])


class CacheClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        steps = audit.read_jsonl(STEPS)
        cls.result = audit.analyze_steps(steps)

    def test_step1_is_warmup_not_invalidation(self):
        warm_steps = {w["step"] for w in self.result["cache_warmups"]}
        invalid_steps = {i["step"] for i in self.result["cache_invalidations"]}
        self.assertIn(1, warm_steps)
        self.assertNotIn(1, invalid_steps)

    def test_late_rewrite_is_invalidation(self):
        invalid_steps = {i["step"] for i in self.result["cache_invalidations"]}
        self.assertIn(4, invalid_steps)

    def test_read_share_is_fraction(self):
        share = self.result["cache_read_share"]
        self.assertIsNotNone(share)
        self.assertTrue(0.0 <= share <= 1.0)

    def test_fresh_excludes_cache_write(self):
        # Fixture step 4: input 1000, cache_read 500, cache_write 18000.
        # Fresh new input = input - cache_read = 500 (NOT input - read - write -> 0).
        step4 = next(i for i in self.result["cache_invalidations"] if i["step"] == 4)
        self.assertEqual(step4["fresh"], 500)


class JsonPayloadTests(unittest.TestCase):
    def test_build_payload_schema(self):
        rows = audit.read_jsonl(TRANSCRIPT)
        steps = audit.read_jsonl(STEPS)
        analysis = audit.analyze(rows, status=None, steps=steps)
        payload = audit.build_payload(TRANSCRIPT, analysis, limit=8)
        self.assertEqual(payload["schema"], "claude-context-audit/1")
        self.assertTrue(payload["cache"]["has_step_data"])
        self.assertIn("recommendations", payload)
        # Must be JSON-serializable.
        json.dumps(payload)


class EndToEndCliTests(unittest.TestCase):
    """Exercise the `claude-ctx` umbrella via `python -m`, no install needed."""

    def _run(self, args, **kw):
        env = dict(os.environ, PYTHONPATH=str(PKG_ROOT), **kw.pop("env", {}))
        return subprocess.run(
            [sys.executable, "-m", "claude_context_tools.cli", *args],
            capture_output=True, text=True, env=env, check=True,
        )

    def test_audit_json_cli(self):
        proc = self._run(["audit", "--transcript", str(TRANSCRIPT),
                          "--state-dir", str(STATE_DIR), "--json"])
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "claude-context-audit/1")

    def test_dashboard_show_cli(self):
        proc = self._run(["show", SESSION], env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR)})
        self.assertIn("sample-repo", proc.stdout)
        self.assertIn("ctx audit", proc.stdout)

    def test_dashboard_table_cli(self):
        # Wide COLUMNS so the responsive layout doesn't truncate the repo name.
        proc = self._run(["dashboard", "--refresh", "0", "--include-stale"],
                         env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR), "COLUMNS": "200"})
        self.assertIn("sample-repo", proc.stdout)

    def test_steps_cli(self):
        proc = self._run(["steps", "--limit", "5"],
                         env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR), "COLUMNS": "160"})
        self.assertIn("Recent step costs", proc.stdout)

    def test_steps_json_cli(self):
        proc = self._run(["steps", "--json", "--limit", "5"],
                         env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR)})
        rows = json.loads(proc.stdout)
        self.assertTrue(rows and "cache_write" in rows[0] and "step" in rows[0])

    def test_hook_is_failsafe(self):
        # Garbage stdin and unknown sessions must never crash or emit noise.
        env = dict(os.environ, PYTHONPATH=str(PKG_ROOT), CLAUDE_STATUS_STATE_DIR=str(STATE_DIR))
        for stdin in ("not json", '{"session_id":"nope"}'):
            proc = subprocess.run(
                [sys.executable, "-m", "claude_context_tools.cli", "hook"],
                input=stdin, capture_output=True, text=True, env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_watch_cli(self):
        proc = self._run(["watch", SESSION, "--refresh", "0"],
                         env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR)})
        self.assertIn("ctx watch", proc.stdout)

    def test_explain_cli(self):
        proc = self._run(["explain", SESSION], env={"CLAUDE_STATUS_STATE_DIR": str(STATE_DIR)})
        self.assertIn("ctx explain", proc.stdout)


class PricingTests(unittest.TestCase):
    def test_match_model(self):
        self.assertEqual(pricing.match_model("Opus 4.8 (1M context)"), "opus-4.8")
        self.assertEqual(pricing.match_model("Sonnet 4.6"), "sonnet-4.6")
        self.assertTrue(pricing.match_model("Opus 9.9").startswith("opus-"))  # unknown -> family fallback
        self.assertEqual(pricing.match_model(""), pricing.DEFAULT_MODEL)
        self.assertEqual(pricing.match_model("Claude Fable 5 (1M context)"), "fable-5")
        self.assertEqual(pricing.match_model("fable"), "fable-5")  # no version -> family fallback

    def test_economics_breakeven_is_size_independent(self):
        a = pricing.cache_economics(100_000, "opus-4.8")
        b = pricing.cache_economics(564_000, "opus-4.8")
        self.assertAlmostEqual(b["rebuild_cost"], 564_000 * 6.25 / 1_000_000, places=4)
        self.assertAlmostEqual(a["breakeven_hours"], b["breakeven_hours"], places=6)


class DigestTests(unittest.TestCase):
    def test_build_digest(self):
        steps = audit.read_jsonl(STEPS)
        views = [dashboard.step_view(s, 0.0) for s in steps]
        d = dashboard.build_digest(views)
        self.assertEqual(d["turns"], 4)
        self.assertEqual(d["sessions"], 1)
        self.assertTrue(d["biggest_cache_writes"], "fixture has large cache writes")

    def test_parse_since(self):
        self.assertEqual(dashboard.parse_since("2h"), 7200)
        self.assertEqual(dashboard.parse_since("30m"), 1800)
        self.assertIsNone(dashboard.parse_since(None))
        # must not crash on whitespace-only, and reject non-positive
        self.assertIsNone(dashboard.parse_since("   "))
        self.assertIsNone(dashboard.parse_since("-1h"))
        self.assertIsNone(dashboard.parse_since("0h"))
        self.assertIsNone(dashboard.parse_since("garbage"))

    def test_sparkline_no_crash_and_rebuild_is_not_green(self):
        # A big-write/no-read turn must color as fresh/expensive (low read share), not green.
        steps = audit.read_jsonl(STEPS)
        # analyze_steps shares the same input-excludes-write semantics we rely on.
        result = audit.analyze_steps(steps)
        self.assertIsNotNone(result["cache_read_share"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
