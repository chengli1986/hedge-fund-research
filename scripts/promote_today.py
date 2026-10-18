#!/usr/bin/env python3
"""Today's auto-promotions that still stand.

wrapper-auto-promote.sh reads logs/auto-promote-history.jsonl three times
after the agent: the commit-message check, the post-commit probe, and the
exit-code reconciliation. Each used to take every "promoted" row of the day,
so a row the first check had reverted was probed (and reverted) again and
still counted as a promotion (stage-6 audit, 2026-10-14). A revert writes a
row of its own ("failed_validation" / "auto_reverted" / "revert_failed"); a
promotion with one later that day no longer stands.

    python3 scripts/promote_today.py      # prints the count, for the wrapper
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
# revert_failed: a check failed but the revert did not go through (aborted);
# the source is still live and needs a person, not another automatic try.
REVERT_OUTCOMES = {"failed_validation", "auto_reverted", "revert_failed"}
# The live-test gate. The commit-message validator checks the agent's claim
# against it; verify_promotions' probe checks what the source measures now.
MIN_ARTICLES = 3
MIN_CONTENT_CHARS = 500
HISTORY = Path(__file__).resolve().parent.parent / "logs" / "auto-promote-history.jsonl"


def live_promotions(log: Path, today: str) -> list[dict]:
    """Today's "promoted" rows, in file order, minus ids reverted today."""
    if not log.exists():
        return []
    rows = []
    for line in log.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("date") == today:
            rows.append(e)
    live: dict = {}
    for e in rows:
        eid = e.get("id")
        if e.get("outcome") == "promoted":
            live[eid] = e
        elif e.get("outcome") in REVERT_OUTCOMES:
            live.pop(eid, None)
    return list(live.values())


def today_bjt() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


if __name__ == "__main__":
    print(len(live_promotions(HISTORY, today_bjt())))
    sys.exit(0)
