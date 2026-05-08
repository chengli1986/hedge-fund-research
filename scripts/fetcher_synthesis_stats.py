#!/usr/bin/env python3
"""Compute fetcher-synthesis success rate over a rolling window.

Reads logs/fetcher-synthesis-history.jsonl and reports:
  - 30-day rolling: success / total
  - 90-day rolling: success / total
  - all-time: success / total
  - per-quality breakdown (HIGH / MEDIUM / LOW)

Usage:
  python3 scripts/fetcher_synthesis_stats.py [--window 30] [--json]

Used to answer "is fetcher-synthesis worth running?" — if rolling success is
< 20%, consider deprioritizing inaccessible funds and putting energy elsewhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE_DIR / "logs" / "fetcher-synthesis-history.jsonl"


def load_history(include_heartbeats: bool = False) -> list[dict]:
    """Load history entries. Heartbeats (session_end markers) are filtered by
    default since they're not synthesis attempts and would skew success rates."""
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not include_heartbeats and entry.get("id") == "_heartbeat":
            continue
        out.append(entry)
    return out


def _within_window(entry: dict, cutoff: datetime) -> bool:
    raw = entry.get("attempted_at") or entry.get("date") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except (ValueError, TypeError):
        return False


def compute_stats(entries: list[dict], window_days: int) -> dict:
    """Counts in window. Multiple attempts on same id all count (each represents
    work done) — caller can dedupe on id if they want unique-fund stats."""
    if not entries:
        return {"window_days": window_days, "total": 0, "success": 0,
                "failed": 0, "success_rate": None, "per_quality": {}}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    in_window = [e for e in entries if _within_window(e, cutoff)]

    outcomes = Counter(e.get("outcome", "unknown") for e in in_window)
    success = outcomes.get("success", 0)
    failed = outcomes.get("failed", 0)
    total = success + failed
    rate = (success / total) if total else None

    per_quality = {}
    for q in ("HIGH", "MEDIUM", "LOW"):
        q_entries = [e for e in in_window if e.get("quality") == q]
        q_success = sum(1 for e in q_entries if e.get("outcome") == "success")
        q_total = sum(1 for e in q_entries if e.get("outcome") in ("success", "failed"))
        per_quality[q] = {
            "success": q_success,
            "total": q_total,
            "rate": (q_success / q_total) if q_total else None,
        }

    # Breakdown by needs_playwright flag — tests whether discovery's shell-HTML
    # detection actually predicts a fund's fetcher-synthesis success. If the two
    # cohorts have similar success rates, the prioritization is noise; if very
    # different, the signal is real.
    needs_pw = {}
    for flag_value, label in [(True, "needs_playwright"), (False, "no_playwright_flag")]:
        cohort = [e for e in in_window if bool(e.get("needs_playwright")) == flag_value]
        c_success = sum(1 for e in cohort if e.get("outcome") == "success")
        c_total = sum(1 for e in cohort if e.get("outcome") in ("success", "failed"))
        needs_pw[label] = {
            "success": c_success,
            "total": c_total,
            "rate": (c_success / c_total) if c_total else None,
        }

    return {
        "window_days": window_days,
        "total": total,
        "success": success,
        "failed": failed,
        "skipped_or_other": len(in_window) - total,
        "success_rate": rate,
        "per_quality": per_quality,
        "needs_playwright_breakdown": needs_pw,
    }


def format_rate(r: float | None) -> str:
    return f"{r * 100:.1f}%" if r is not None else "n/a"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=30,
                        help="primary window in days (default 30)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of human-readable text")
    args = parser.parse_args()

    entries = load_history()
    primary = compute_stats(entries, args.window)
    secondary = compute_stats(entries, 90)
    alltime = compute_stats(entries, 365 * 100)  # essentially all-time

    if args.json:
        print(json.dumps({
            f"{args.window}d": primary,
            "90d": secondary,
            "alltime": alltime,
            "history_entries": len(entries),
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"=== Fetcher Synthesis Stats ({len(entries)} history entries) ===")
    for label, stats in (
        (f"{args.window}d rolling", primary),
        ("90d rolling", secondary),
        ("All-time", alltime),
    ):
        rate_str = format_rate(stats["success_rate"])
        print(f"\n{label}: {stats['success']}/{stats['total']} success "
              f"= {rate_str}")
        for q, qstats in stats["per_quality"].items():
            qrate = format_rate(qstats["rate"])
            if qstats["total"]:
                print(f"  {q:6s}: {qstats['success']}/{qstats['total']} = {qrate}")
        # needs_playwright cohort comparison (only for primary window — secondary
        # covers it too but we don't repeat to keep output readable)
        if label.startswith(f"{args.window}d"):
            np = stats["needs_playwright_breakdown"]
            yes = np["needs_playwright"]
            no = np["no_playwright_flag"]
            if yes["total"] or no["total"]:
                print(f"  needs_playwright=true:  {yes['success']}/{yes['total']} = "
                      f"{format_rate(yes['rate'])}")
                print(f"  needs_playwright=false: {no['success']}/{no['total']} = "
                      f"{format_rate(no['rate'])}")
                if yes["total"] >= 3 and no["total"] >= 3 and yes["rate"] and no["rate"]:
                    if abs(yes["rate"] - no["rate"]) < 0.1:
                        print("  → flag adds little signal (cohorts within 10pp)")

    if primary["total"] == 0:
        print("\n⚠️ no synthesis attempts in primary window — nothing to evaluate")
    elif primary["success_rate"] is not None and primary["success_rate"] < 0.2:
        print("\n⚠️ rolling success rate < 20% — consider deprioritizing "
              "fetcher-synthesis until program tweaks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
