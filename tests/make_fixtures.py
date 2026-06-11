#!/usr/bin/env python3
"""Generate sanitized, synthetic fixtures for the context-tool smoke tests.

Everything here is invented. No real transcript content, paths, or secrets are
used. Re-run this to regenerate the committed fixtures after changing shapes:

    python3 tests/make_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION = "00000000-aaaa-bbbb-cccc-000000000001"
# Where the transcript is actually written on disk (machine-specific, not committed
# into any fixture). The CLI tests pass --transcript explicitly, so the status
# records only need a *stable, synthetic* transcript_path — baking the absolute
# local path here is what made regenerated fixtures churn per machine.
TRANSCRIPT = FIXTURES / "transcript" / f"{SESSION}.jsonl"
TRANSCRIPT_PATH_FIELD = f"/synthetic/claude/projects/sample-repo/{SESSION}.jsonl"

# A clearly-synthetic "log" big enough to trip the large-result threshold (8000 chars).
FAIL_LINE = "FAILED tests/test_widget.py::test_case_{0} - AssertionError: expected 1 got 0\n"
BIG_LOG = "".join(FAIL_LINE.format(i) for i in range(160))
FILE_BODY = "def authenticate(user, token):\n    # synthetic sample body\n    return verify(user, token)\n" * 8
# Read once, large, and its path never referenced again -> "loaded but unused".
DEAD_BODY = "".join(f"def orphan_helper_{i}():\n    return None  # never referenced again\n" for i in range(60))


def transcript_rows() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user",
            "content": "Please refactor the auth module and run the tests."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I should read the auth file before editing it. " * 6},
            {"type": "text", "text": "I'll read the auth module first."},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/src/auth.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": FILE_BODY},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": BIG_LOG},
        ]}},
        # Read a legacy file once; nothing afterward ever mentions its path -> dead context.
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Let me glance at the legacy helpers too."},
            {"type": "tool_use", "id": "t5", "name": "Read", "input": {"file_path": "/repo/legacy/orphan_module.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t5", "content": DEAD_BODY},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Let me re-read the same file to double-check."},
            {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/repo/src/auth.py"}},
        ]}},
        # Identical Read result -> repeated read AND duplicate blob.
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": FILE_BODY},
        ]}},
        # Attachment row (system-injected context).
        {"type": "attachment", "attachment": {"type": "skill_listing",
            "text": "Available skills: alpha, beta, gamma. " * 20}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t4", "name": "Agent",
                "input": {"description": "investigate token refresh", "prompt": "..."}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t4",
                "content": "Subagent report: token refresh lives in auth/refresh.py. " * 12},
        ]}},
    ]


def status_record() -> dict:
    return {
        "session_id": SESSION,
        "updated_at": 1700000000,
        "model": "Opus 4.8 (1M context)",
        "agent": "",
        "repo": "sample-repo",
        "cwd": "/repo",
        "project_dir": "/repo",
        "context_pct": 12,
        "context_size": 1000000,
        "rate_pct": 1,
        "input_tokens": 50000,
        "output_tokens": 1200,
        "cache_read_tokens": 47000,
        "cache_write_tokens": 3000,
        "total_tokens": 51200,
        "cost_usd": 0.42,
        "duration_ms": 600000,
        "transcript_path": TRANSCRIPT_PATH_FIELD,
    }


def step_rows() -> list[dict]:
    base = {"session_id": SESSION, "model": "Opus 4.8 (1M context)", "repo": "sample-repo",
            "transcript_path": TRANSCRIPT_PATH_FIELD, "context_pct": 5}
    return [
        # Step 1: pure warm-up — big write, no reads yet.
        {**base, "step_no": 1, "delta_input_tokens": 600, "delta_output_tokens": 120,
            "delta_cache_read_tokens": 0, "delta_cache_write_tokens": 22000, "delta_tokens": 720},
        # Steps 2-3: healthy cache hits (cache becomes warm).
        {**base, "step_no": 2, "delta_input_tokens": 22500, "delta_output_tokens": 300,
            "delta_cache_read_tokens": 22000, "delta_cache_write_tokens": 200, "delta_tokens": 500},
        {**base, "step_no": 3, "delta_input_tokens": 23000, "delta_output_tokens": 300,
            "delta_cache_read_tokens": 22200, "delta_cache_write_tokens": 150, "delta_tokens": 450},
        # Step 4: cache was warm, then largely rewritten -> invalidation.
        {**base, "step_no": 4, "delta_input_tokens": 1000, "delta_output_tokens": 200,
            "delta_cache_read_tokens": 500, "delta_cache_write_tokens": 18000, "delta_tokens": 200},
    ]


def main() -> None:
    # Mirror the real layout: a state dir holding <session>.json plus a steps/
    # subdir, with transcripts kept separately (like ~/.claude/projects).
    state = FIXTURES / "state"
    (FIXTURES / "transcript").mkdir(parents=True, exist_ok=True)
    (state / "steps").mkdir(parents=True, exist_ok=True)

    with (FIXTURES / "transcript" / f"{SESSION}.jsonl").open("w", encoding="utf-8") as fh:
        for row in transcript_rows():
            fh.write(json.dumps(row) + "\n")

    (state / f"{SESSION}.json").write_text(
        json.dumps(status_record(), indent=2) + "\n", encoding="utf-8")

    with (state / "steps" / f"{SESSION}.jsonl").open("w", encoding="utf-8") as fh:
        for row in step_rows():
            fh.write(json.dumps(row) + "\n")

    print(f"wrote fixtures under {FIXTURES}")


if __name__ == "__main__":
    main()
