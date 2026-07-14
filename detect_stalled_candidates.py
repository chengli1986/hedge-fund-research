#!/usr/bin/env python3
"""Post-pipeline sweep: auto-route candidates stuck without progress.

Runs after the daily discover -> screen -> entrypoint stages (and the guard
step, which stamps status_since for the agent's own legal edits). Two rules,
both gated on status_since >= --threshold-days:

  - seed (with a confirmed research_url) -> discovered
    Unsticks the stage1 JS-blind-spot case (2026-07-14 Nuveen/Longleaf/Lord
    Abbett/Invesco incident): stage1's httpx crawl can't see JS-rendered nav
    links, so a seed candidate whose research_url was already confirmed by a
    human or the discovery agent just sits in "seed" forever with no retry
    path. Forcing it to "discovered" lets it flow through screen_fund_
    candidates.py again next run; if it still can't progress it naturally
    falls into the screen_failed rule below within a few more days.

  - screen_failed -> inaccessible + needs_playwright
    screen_fund_candidates.py already retries screen_failed candidates every
    day on its own, but a candidate that keeps failing the same way for days
    is never going to succeed via a static httpx fetch. Skip the pointless
    retries and hand it straight to the weekly fetcher-synthesis queue
    (which only picks up status="inaccessible").

Every auto-action tags candidate.notes with an [auto-stall YYYY-MM-DD]
marker for audit, same convention as guard_candidate_status.py's
[guard: ...] marker.

Usage:
  python3 detect_stalled_candidates.py
  python3 detect_stalled_candidates.py --dry-run
  python3 detect_stalled_candidates.py --threshold-days 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from status_util import days_since, set_status, tag_note

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"

DEFAULT_THRESHOLD_DAYS = 3


def load_candidates() -> list[dict]:
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))


def save_candidates(candidates: list[dict]) -> None:
    """Atomically write candidates to fund_candidates.json."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=CANDIDATES_FILE.parent,
        prefix=".fund_candidates_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, CANDIDATES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def find_stalled(candidates: list[dict], threshold_days: int) -> list[dict]:
    """Return [{id, from_status, new_status, days_stuck, needs_playwright}]
    for candidates that should be auto-routed."""
    actions = []
    for c in candidates:
        days = days_since(c)
        if days is None or days < threshold_days:
            continue
        status = c.get("status")
        if status == "seed" and c.get("research_url"):
            actions.append({
                "id": c["id"], "from_status": status, "new_status": "discovered",
                "days_stuck": days, "needs_playwright": False,
            })
        elif status == "screen_failed":
            actions.append({
                "id": c["id"], "from_status": status, "new_status": "inaccessible",
                "days_stuck": days, "needs_playwright": True,
            })
    return actions


def apply_stall_actions(candidates: list[dict], actions: list[dict]) -> int:
    """Apply each action to its candidate in place. Returns count applied."""
    by_id = {c["id"]: c for c in candidates}
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    applied = 0
    for a in actions:
        c = by_id.get(a["id"])
        if c is None:
            continue
        set_status(c, a["new_status"])
        if a["needs_playwright"]:
            c["needs_playwright"] = True
        tag_note(
            c,
            f"[auto-stall {today}] {a['from_status']}->{a['new_status']} "
            f"after {a['days_stuck']}d stuck",
        )
        applied += 1
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold-days", type=int, default=DEFAULT_THRESHOLD_DAYS,
        help=f"Days without progress before auto-routing (default: {DEFAULT_THRESHOLD_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing fund_candidates.json",
    )
    args = parser.parse_args(argv)

    candidates = load_candidates()
    actions = find_stalled(candidates, args.threshold_days)

    if not actions:
        print(f"stall: 0 candidate(s) stuck >= {args.threshold_days}d")
        return 0

    for a in actions:
        print(f"STALL {a['id']}: {a['from_status']} -> {a['new_status']} "
              f"(stuck {a['days_stuck']}d){' +needs_playwright' if a['needs_playwright'] else ''}")

    if args.dry_run:
        print(f"stall: {len(actions)} candidate(s) would be auto-routed (dry-run, not saved)")
        return 0

    applied = apply_stall_actions(candidates, actions)
    save_candidates(candidates)
    print(f"stall: {applied} candidate(s) auto-routed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
