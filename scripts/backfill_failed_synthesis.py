#!/usr/bin/env python3
"""Auto-mark unprocessed fetcher-synthesis targets as failed.

The fetcher-synthesis agent is supposed to write ``synthesis_outcome`` for each
target it handles, but it can silently skip a target (it "picks easy ones" and
leaves a hard candidate untouched) or crash without recording anything. When
that happens the candidate stays ``inaccessible`` with no history entry, so
``synthesize_fetchers.auto_reject_exhausted_candidates`` never counts a failure
and the candidate re-queues every week with no record and no alert — the
franklin-templeton "stuck 8 weeks" failure mode.

This runs AFTER the agent. Given the session's planned target ids (the first
``SESSION_LIMIT`` candidates ``synthesize_fetchers`` listed) and the session
start timestamp, it marks any planned target that is STILL ``inaccessible`` and
was NOT attempted this session (``synthesis_attempted_at`` missing or older than
``run_start``) as ``synthesis_outcome="failed"``. Those failures then flow into
``logs/fetcher-synthesis-history.jsonl`` via ``sync_synthesis_history.py`` and
into ``auto_reject``'s 3-strike counter, and the count is reported to the
session heartbeat so a fully-unproductive session raises an alert.

Usage:
  python3 scripts/backfill_failed_synthesis.py \
    --planned-ids franklin-templeton,cohen-steers \
    --run-start 2026-07-05T18:00:00Z

Exit codes:
  0 = ran (regardless of whether anything was backfilled)
  1 = unrecoverable error
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def backfill(planned_ids: list[str], run_start: str,
             candidates_path: Path | None = None) -> dict:
    """Mark planned targets not attempted this session as failed.

    A planned target is backfilled to ``synthesis_outcome="failed"`` when it is
    still ``inaccessible`` AND its ``synthesis_attempted_at`` is missing or
    predates ``run_start`` (i.e. the agent neither succeeded — which would move
    it off ``inaccessible`` — nor recorded an attempt this session).

    Returns ``{"backfilled": [ids], "backfilled_count": n}``. Writes back only
    when at least one candidate changed (preserves mtime otherwise).
    """
    # Resolve at call time (not as a default arg) so tests can monkeypatch
    # CANDIDATES_FILE — a bound default would pin the real production path.
    if candidates_path is None:
        candidates_path = CANDIDATES_FILE
    run_start_dt = _parse_iso(run_start)
    data = json.loads(candidates_path.read_text())
    candidates = data if isinstance(data, list) else data.get("candidates", [])
    planned = set(planned_ids)
    now = datetime.now(timezone.utc).isoformat()

    backfilled: list[str] = []
    for c in candidates:
        if c.get("id") not in planned:
            continue
        # Agent succeeded → candidate no longer inaccessible → leave it.
        if c.get("status") != "inaccessible":
            continue
        # Agent recorded an attempt at/after session start → leave it.
        attempted_dt = _parse_iso(c.get("synthesis_attempted_at"))
        if run_start_dt is not None and attempted_dt is not None \
                and attempted_dt >= run_start_dt:
            continue
        # Otherwise the agent skipped / silently failed this target → mark failed.
        c["synthesis_attempted_at"] = now
        c["synthesis_outcome"] = "failed"
        # This is the one thing we actually verified above, so it is safe to
        # state as the cause. Anything more specific (403 / selector / timeout)
        # would be a guess — the agent left no record to read.
        # run_start trimmed to seconds: the digest cell budgets 120 chars and a
        # cut reason loses the log pointer that makes it actionable.
        c["synthesis_failure_reason"] = (
            f"agent 未在本次 session 记录尝试（run_start={run_start[:19]}），"
            "由 backfill 判失败；详见 gmia-fetcher-synthesis.log")
        backfilled.append(c["id"])

    if backfilled:
        out = candidates if isinstance(data, list) else {**data, "candidates": candidates}
        candidates_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    return {"backfilled": backfilled, "backfilled_count": len(backfilled)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planned-ids", default="",
                        help="comma-separated candidate ids this session should have handled")
    parser.add_argument("--run-start", required=True,
                        help="session start timestamp (UTC ISO)")
    args = parser.parse_args()

    planned_ids = [x.strip() for x in args.planned_ids.split(",") if x.strip()]
    if not planned_ids:
        print("[backfill] no planned targets — nothing to check")
        return 0

    try:
        result = backfill(planned_ids, args.run_start)
    except Exception as exc:
        print(f"ERROR: backfill failed: {exc}", file=sys.stderr)
        return 1

    n = result["backfilled_count"]
    if n == 0:
        print("[backfill] all planned targets were attempted this session")
    else:
        print(f"[backfill] marked {n} unprocessed target(s) as failed: "
              f"{', '.join(result['backfilled'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
