#!/usr/bin/env python3
"""After a synthesis session: did the agent try a listing template first?

fetcher-synthesis/program.md (Phase 1.5, 2026-10-13) requires every processed
candidate to run scripts/try_template.py --record before any fetch_<id> is
written. The playbook saying so is not a check: this reads what the script
recorded, not what the agent says it did.

A planned candidate counts as processed when synthesis_attempted_at falls in
this session. For each one:
  - no template attempt recorded in this session           -> violation
  - a template passed, yet fetch_<id> is in FETCHERS        -> violation
  - every attempt failed, a fetcher was written, and
    synthesis_template_skip_reason is empty                 -> violation

    python3 scripts/check_template_first.py --planned-ids a,b --run-start 2026-10-18T18:00:01Z

Exit 1 when there is a violation (the wrapper turns it into a non-zero
session so cron-wrapper mails), 0 otherwise. Never edits anything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def _when(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetcher_ids(path: Path) -> set[str]:
    """Keys of the FETCHERS dict, read as text (importing fetch_articles would
    pull in playwright and every fetcher just to list names)."""
    try:
        text = path.read_text()
    except OSError:
        return set()
    start = text.find("FETCHERS = {")
    if start < 0:
        return set()
    body = text[start:text.find("\n}", start)]
    return set(re.findall(r'^\s*"([\w-]+)"\s*:', body, re.M))


def violations(candidates: list[dict], planned: list[str], run_start: datetime,
               fetcher_ids: set[str]) -> list[str]:
    out = []
    by_id = {c["id"]: c for c in candidates}
    for cid in planned:
        c = by_id.get(cid)
        attempted = _when(c.get("synthesis_attempted_at")) if c else None
        if not c or attempted is None or attempted < run_start:
            continue                      # not processed this session: backfill's business
        tries = [a for a in c.get("template_attempts") or []
                 if (_when(a.get("at")) or datetime.min.replace(tzinfo=timezone.utc)) >= run_start]
        wrote = cid in fetcher_ids
        if not tries:
            out.append(f"{cid}: processed without a template attempt (scripts/try_template.py --record)")
        elif any(a.get("ok") for a in tries) and wrote:
            out.append(f"{cid}: a template passed, yet fetch_{cid.replace('-', '_')} was written")
        elif wrote and not (c.get("synthesis_template_skip_reason") or "").strip():
            out.append(f"{cid}: fetcher written after failed templates, but no synthesis_template_skip_reason")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="check the synthesis template-first rule")
    ap.add_argument("--planned-ids", required=True, help="comma-separated")
    ap.add_argument("--run-start", required=True)
    ap.add_argument("--candidates-file", default=str(BASE_DIR / "config" / "fund_candidates.json"))
    a = ap.parse_args(argv)

    start = _when(a.run_start)
    if start is None:
        print(f"ERROR: bad --run-start {a.run_start!r}", file=sys.stderr)
        return 2
    data = json.loads(Path(a.candidates_file).read_text())
    candidates = data if isinstance(data, list) else data.get("candidates", [])
    planned = [x for x in a.planned_ids.split(",") if x]
    found = violations(candidates, planned, start, fetcher_ids(BASE_DIR / "fetch_articles.py"))
    for line in found:
        print(f"TEMPLATE-FIRST VIOLATION: {line}")
    if not found:
        print(f"template-first: ok ({len(planned)} planned)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
