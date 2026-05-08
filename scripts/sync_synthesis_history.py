#!/usr/bin/env python3
"""Reconcile fetcher-synthesis outcomes into a time-series log.

Without this, synthesis history is only visible as the latest snapshot in
fund_candidates.json (synthesis_attempted_at + synthesis_outcome). We can't
answer "what's the 30-day rolling success rate?" — and that's the whole basis
for deciding whether fetcher-synthesis is working.

This script reads fund_candidates.json, finds entries with synthesis_attempted_at
within the past N days that aren't yet logged in
logs/fetcher-synthesis-history.jsonl, and appends them.

Idempotent — running twice on the same day produces no duplicate entries.

Usage:
  python3 scripts/sync_synthesis_history.py [--lookback-days 7]

Exit codes:
  0 = success (regardless of whether anything was written)
  1 = unrecoverable error
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
HISTORY_FILE = BASE_DIR / "logs" / "fetcher-synthesis-history.jsonl"
DEFAULT_LOOKBACK = 7  # days to scan in fund_candidates for un-logged outcomes


def load_candidates() -> list[dict]:
    data = json.loads(CANDIDATES_FILE.read_text())
    return data if isinstance(data, list) else data.get("candidates", [])


def load_history_ids_by_date(history_path: Path) -> set[tuple[str, str]]:
    """Return set of (date, id) pairs already in history. Used for dedup."""
    if not history_path.exists():
        return set()
    seen = set()
    for line in history_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            date_str = e.get("date") or e.get("attempted_at", "")[:10]
            seen.add((date_str, e.get("id", "")))
        except json.JSONDecodeError:
            continue
    return seen


def _commit_sha_for_candidate(fund_id: str, attempted_at: str) -> str:
    """Try to find the git commit that wrote this synthesis attempt.
    Looks for commits within ±1 day of attempted_at touching fund_candidates.json
    or fetch_articles.py with fund_id in the message. Returns '' on miss."""
    try:
        attempted_dt = datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""

    since = (attempted_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    until = (attempted_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", f"--until={until}",
             "--format=%H %s", "--", "fetch_articles.py", "config/fund_candidates.json"],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=10,
        )
        if proc.returncode != 0:
            return ""
        for line in proc.stdout.splitlines():
            sha, _, subject = line.partition(" ")
            # Match common synthesis commit patterns: feat(fetcher): or auto-synthesize
            if "fetcher" in subject.lower() and (
                fund_id in subject or fund_id.replace("-", " ") in subject.lower()
            ):
                return sha
        # Fallback — first commit in window
        first = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else ""
        return first.split(" ", 1)[0] if first else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def reconcile(lookback_days: int = DEFAULT_LOOKBACK) -> dict:
    """Find candidates whose synthesis_attempted_at falls within lookback window
    but isn't yet in history.jsonl, and append them. Returns summary dict."""
    candidates = load_candidates()
    seen = load_history_ids_by_date(HISTORY_FILE)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    appended = []
    for c in candidates:
        attempted = c.get("synthesis_attempted_at")
        outcome = c.get("synthesis_outcome")
        if not attempted or not outcome:
            continue
        try:
            attempted_dt = datetime.fromisoformat(attempted.replace("Z", "+00:00"))
            if attempted_dt.tzinfo is None:
                attempted_dt = attempted_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if attempted_dt < cutoff:
            continue

        date_str = attempted_dt.date().isoformat()
        if (date_str, c["id"]) in seen:
            continue

        entry = {
            "date": date_str,
            "attempted_at": attempted,
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "outcome": outcome,
            "commit": _commit_sha_for_candidate(c["id"], attempted),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "quality": c.get("quality", "?"),
            # Track whether discovery flagged this candidate as needs_playwright,
            # so stats can compare success rates with vs without that flag and
            # tell us if the discovery-side detection actually predicts which
            # candidates fetcher-synthesis should prioritize.
            "needs_playwright": bool(c.get("needs_playwright")),
        }
        appended.append(entry)
        seen.add((date_str, c["id"]))

    if appended:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        with HISTORY_FILE.open("a") as f:
            for entry in appended:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"appended_count": len(appended), "appended": appended}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK)
    args = parser.parse_args()

    try:
        result = reconcile(lookback_days=args.lookback_days)
    except Exception as exc:
        print(f"ERROR: reconcile failed: {exc}", file=sys.stderr)
        return 1

    n = result["appended_count"]
    if n == 0:
        print("[sync-history] no new synthesis outcomes to log")
    else:
        print(f"[sync-history] appended {n} entries to {HISTORY_FILE.name}:")
        for e in result["appended"]:
            commit_str = f" ({e['commit'][:8]})" if e["commit"] else ""
            print(f"  {e['date']} {e['id']:30s} → {e['outcome']}{commit_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
