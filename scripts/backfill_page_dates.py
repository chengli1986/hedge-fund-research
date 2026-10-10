#!/usr/bin/env python3
"""One-off: date the stored undated / year-only rows from their article pages.

Stage 2 now does this for every new article it fetches (fill_date_from_page,
stage-4 audit items 7/15). The rows already fetched before that -- Capital
Group's 25 undated, de-shaw's 10 year-only -- are not fetched again, so this
opens each page once, under isolated_content_dir (the stored bodies are not
touched), and applies the same rule.

Two steps, so what is written is exactly the list that was reviewed:

    python3 scripts/backfill_page_dates.py --propose   # fetch, save + print the list
    python3 scripts/backfill_page_dates.py --apply     # write that saved list, nothing else

--apply fetches nothing. It writes under the store's lock, re-reading the
store inside it, and skips any row that changed since the list was made (a
date was filled meanwhile, or the row is gone).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import fetch_content as fc  # noqa: E402
import jsonl_store  # noqa: E402

SOURCES = ("capital-group", "de-shaw")
PROPOSAL = BASE_DIR / "logs" / "backfill-page-dates.json"
DATE_FIELDS = ("date", "date_listed", "date_basis")


def candidates(rows: list[dict]) -> list[dict]:
    """Rows fill_date_from_page may date: no date, or a year-only YYYY-01-01."""
    def open_date(r: dict) -> bool:
        date, raw = (r.get("date") or "").strip(), (r.get("date_raw") or "").strip()
        return not date or (len(raw) == 4 and raw.isdigit() and date == f"{raw}-01-01")
    return [r for r in rows if r.get("source_id") in SOURCES and not r.get("date_basis") and open_date(r)]


def propose(data: Path, out: Path, today: str, fetch=fc.fetch_with_evidence) -> list[dict]:
    rows, damaged = jsonl_store.read_rows(data)
    if damaged:
        raise SystemExit(f"{damaged} damaged row(s) in the store; stopping")
    found = []
    for r in candidates(rows):
        with fc.isolated_content_dir("gmia-backfill-"):
            _, evidence = fetch(r, fc.content_fetcher_for(r))
        day = evidence.get("page_date")
        new = dict(r)
        ok = fc.fill_date_from_page(new, day, today)
        found.append({"id": r["id"], "source_id": r["source_id"], "title": r.get("title", ""),
                      "url": r.get("url", ""), "was": r.get("date"), "date_raw": r.get("date_raw"),
                      "fetched_at": r.get("fetched_at"), "page_date": day,
                      "new": {k: new.get(k) for k in DATE_FIELDS} if ok else None})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    return found


def apply(data: Path, proposal: Path) -> tuple[int, int]:
    items = {p["id"]: p for p in json.loads(proposal.read_text(encoding="utf-8")) if p.get("new")}
    written = skipped = 0
    with jsonl_store.file_lock(data):
        rows, damaged = jsonl_store._read_locked(data)
        if damaged:
            raise SystemExit(f"{damaged} damaged row(s) in the store; stopping without writing")
        seen = set()
        for r in rows:
            p = items.get(r.get("id"))
            if p is None:
                continue
            seen.add(r["id"])
            if r.get("date_basis") or r.get("date") != p["was"]:
                skipped += 1
                continue
            r.update(p["new"])
            written += 1
        skipped += len(set(items) - seen)
        if written:
            jsonl_store._rewrite_locked(data, rows, None)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--propose", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--data", type=Path, default=fc.DATA_FILE)
    ap.add_argument("--proposal", type=Path, default=PROPOSAL)
    args = ap.parse_args(argv)
    if args.apply:
        written, skipped = apply(args.data, args.proposal)
        print(f"written: {written}, skipped (changed since the list was made): {skipped}")
        return 0
    found = propose(args.data, args.proposal, datetime.now(fc.BJT).strftime("%Y-%m-%d"))
    for p in found:
        new = p["new"]["date"] if p["new"] else f"-- not filled (page: {p['page_date']})"
        print(f"{p['source_id']:14} {str(p['was'] or '(none)'):11} -> {new:12} {p['title'][:70]}")
    print(f"{sum(1 for p in found if p['new'])} of {len(found)} would be dated; list saved to {args.proposal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
