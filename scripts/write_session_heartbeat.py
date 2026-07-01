#!/usr/bin/env python3
"""Write a session-end heartbeat to fetcher-synthesis-history.jsonl.

This is the C-tier "monitoring of monitoring" — without a heartbeat per session,
an agent that crashed silently or stopped writing fund_candidates.json fields
would yield zero history entries and stats would lie ("0 attempts in 30d").
With heartbeat, we can detect "session ran but no outcomes" and alert.

Usage:
  python3 scripts/write_session_heartbeat.py \
    --targets-count 2 --reconcile-appended 0 --agent-exit 0

Exit codes:
  0 = heartbeat written, no inconsistency
  1 = inconsistency detected: agent had targets but recorded 0 outcomes
  2 = file write failed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE_DIR / "logs" / "fetcher-synthesis-history.jsonl"


def write_heartbeat(targets_count: int, reconcile_appended: int,
                    agent_exit: int, history_path: Path = HISTORY_FILE,
                    backfilled_count: int = 0) -> dict:
    """Append a heartbeat entry. Returns the entry dict for inspection."""
    entry = {
        "date": datetime.now(BJT).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(BJT).isoformat(),
        "id": "_heartbeat",
        "outcome": "session_end",
        "targets_count": int(targets_count),
        "reconcile_appended": int(reconcile_appended),
        "agent_exit": int(agent_exit),
        "backfilled_count": int(backfilled_count),
    }
    history_path.parent.mkdir(exist_ok=True)
    with history_path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def detect_inconsistency(targets_count: int, reconcile_appended: int,
                         agent_exit: int, backfilled_count: int = 0) -> str | None:
    """Return reason string if state is inconsistent, else None.

    Two signals, either → alert:
    1. backfill_failed_synthesis marked >=1 planned target as failed → the agent
       skipped or silently failed work it should have done this session (the
       franklin-templeton "stuck, never processed, never alerted" failure mode).
    2. (fallback) agent claimed success (exit 0) on a non-empty target list but
       reconcile found nothing to log — schema drift or a silent failure the
       backfill step didn't cover.
    """
    if backfilled_count > 0:
        return (f"{backfilled_count} planned target(s) went unprocessed and were "
                f"auto-marked failed — agent skipped or silently failed them")
    if targets_count > 0 and reconcile_appended == 0 and agent_exit == 0:
        return (f"agent ran with {targets_count} target(s) but reconcile "
                f"appended 0 entries — schema drift or silent agent failure")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets-count", type=int, required=True,
                        help="how many candidates were available at session start")
    parser.add_argument("--reconcile-appended", type=int, required=True,
                        help="how many entries sync_synthesis_history.py wrote")
    parser.add_argument("--agent-exit", type=int, required=True,
                        help="agent process exit code")
    parser.add_argument("--backfilled-count", type=int, default=0,
                        help="how many planned targets backfill_failed_synthesis marked failed")
    args = parser.parse_args()

    try:
        entry = write_heartbeat(args.targets_count, args.reconcile_appended,
                                args.agent_exit,
                                backfilled_count=args.backfilled_count)
    except OSError as exc:
        print(f"ERROR: heartbeat write failed: {exc}", file=sys.stderr)
        return 2

    print(f"[heartbeat] wrote session_end entry: "
          f"targets={entry['targets_count']} appended={entry['reconcile_appended']} "
          f"backfilled={entry['backfilled_count']} agent_exit={entry['agent_exit']}")

    reason = detect_inconsistency(args.targets_count, args.reconcile_appended,
                                  args.agent_exit,
                                  backfilled_count=args.backfilled_count)
    if reason:
        print(f"[heartbeat] ⚠️ INCONSISTENCY: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
