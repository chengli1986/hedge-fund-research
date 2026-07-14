#!/usr/bin/env python3
"""One-time backfill: give every pre-existing candidate a status_since value.

status_since didn't exist before 2026-07-15, so candidates from before that
have no record of when they entered their current status. There's no exact
history to recover, so this falls back to the closest available last_*_at
field as an approximation (good enough to unblock stall detection and the
email "days stuck" badges — see [[hedge-fund-research]]):

  last_validated_at > last_screened_at > last_discovered_at > last_deep_analyzed_at

Idempotent: a candidate that already has status_since is left untouched, so
this is safe to re-run (e.g. after adding new candidates that also predate
status_since some other way).

Usage:
  python3 backfill_status_since.py            # apply
  python3 backfill_status_since.py --dry-run   # preview only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"

FALLBACK_FIELDS = (
    "last_validated_at",
    "last_screened_at",
    "last_discovered_at",
    "last_deep_analyzed_at",
)


def load_candidates() -> list[dict]:
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))


def save_candidates(candidates: list[dict]) -> None:
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


def backfill_one(candidate: dict) -> bool:
    """Set candidate["status_since"] from the best available fallback field.

    Returns True if a value was set, False if already present or no
    fallback field was available.
    """
    if candidate.get("status_since"):
        return False
    for field in FALLBACK_FIELDS:
        value = candidate.get(field)
        if value:
            candidate["status_since"] = value
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing fund_candidates.json",
    )
    args = parser.parse_args(argv)

    candidates = load_candidates()
    backfilled, no_fallback = [], []
    for c in candidates:
        had_since = bool(c.get("status_since"))
        if backfill_one(c):
            backfilled.append(c["id"])
        elif not had_since:
            no_fallback.append(c["id"])

    for cid in backfilled:
        c = next(x for x in candidates if x["id"] == cid)
        print(f"BACKFILL {cid}: status_since={c['status_since']}")
    for cid in no_fallback:
        print(f"NO FALLBACK {cid}: no last_*_at field available, left unset")

    print(f"backfill: {len(backfilled)} set, {len(no_fallback)} left unset "
          f"(no fallback), {len(candidates) - len(backfilled) - len(no_fallback)} already had it")

    if not args.dry_run and backfilled:
        save_candidates(candidates)
    elif args.dry_run and backfilled:
        print("(dry-run: not saved)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
