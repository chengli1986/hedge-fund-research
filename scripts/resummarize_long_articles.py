#!/usr/bin/env python3
"""Re-summarise published articles that the old 15,000-char limit cut short.

One-off for 2026-10-06, kept for the next time MAX_CONTENT_CHARS changes.
MAX_CONTENT_CHARS went from 15,000 to 80,000 (2fea5ac); 305 summarised
articles were longer than 15,000 characters, so their summaries were written
from part of the text. The user approved re-summarising them.

Safety, because this replaces summaries already on the page:
- the same path as the nightly run: analyze_articles._analyze_with_fallback
  (model chain, retries, check_grounding, usage logging);
- a summary is replaced ONLY by a new summary that came back whole; a
  decline, a grounding failure or an API error keeps the old one and is
  listed in the report -- an article never loses its summary here;
- every replaced summary is written to a backup file first;
- writes go through jsonl_store under its lock, a few articles at a time.

    python3 scripts/resummarize_long_articles.py --dry-run
    python3 scripts/resummarize_long_articles.py --limit 3 --backup DIR
    python3 scripts/resummarize_long_articles.py --backup DIR
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import analyze_articles as aa  # noqa: E402
import jsonl_store  # noqa: E402

OLD_LIMIT = 15000
SUMMARY_FIELDS = ("summary_en", "summary_zh", "key_takeaway_en", "key_takeaway_zh", "themes", "analysis_model")
SAVE_EVERY = 5


def candidates(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r.get("summarized") or r.get("content_status") != "ok":
            continue
        try:
            n = len(aa._resolve_content_path(r).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if n > OLD_LIMIT and not r.get("analysis_resummarized_at"):
            out.append(r)
    return out


def apply_result(row: dict, result: dict | None) -> str:
    """Replace the summary only with a whole new one. Returns what happened."""
    if result is None:
        return "kept: no answer from any model"
    if result.get("insufficient_content"):
        return f"kept: declined ({(result.get('reason') or '')[:160]})"
    for field in ("summary_en", "summary_zh", "key_takeaway_en", "key_takeaway_zh"):
        if not str(result.get(field) or "").strip():
            return f"kept: new answer had no {field}"
    if not result.get("themes"):
        return "kept: new answer had no theme"
    row["summary_en"], row["summary_zh"] = result["summary_en"], result["summary_zh"]
    row["key_takeaway_en"], row["key_takeaway_zh"] = result["key_takeaway_en"], result["key_takeaway_zh"]
    row["themes"] = result["themes"]
    row["analysis_model"] = result.get("_model")
    row["analysis_resummarized_at"] = datetime.now(aa.BJT).isoformat(timespec="seconds")
    return "replaced"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--backup", type=Path, help="directory for the old summaries and the report")
    args = ap.parse_args(argv)

    rows, damaged = jsonl_store.read_rows(aa.DATA_FILE)
    if damaged:
        print(f"{damaged} damaged row(s) in the store; refusing to rewrite it")
        return 1
    todo = candidates(rows)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} articles to re-summarise (limit {aa.MAX_CONTENT_CHARS:,} chars)")
    if args.dry_run or not todo:
        return 0
    if not args.backup:
        ap.error("--backup DIR is required for a real run")
    args.backup.mkdir(parents=True, exist_ok=True)
    keys = aa._load_api_keys()
    report = args.backup / "report.jsonl"
    old = args.backup / "old-summaries.jsonl"
    started, outcomes, pending_ids = time.time(), {}, []
    by_id = {r["id"]: r for r in rows}

    def flush():
        if not pending_ids:
            return
        # read and rewrite under one lock: the new summaries go onto the store
        # as it is now, so nothing written meanwhile by another job is lost
        with jsonl_store.file_lock(aa.DATA_FILE):
            fresh, bad = jsonl_store._read_locked(aa.DATA_FILE)
            if bad:
                raise SystemExit(f"{bad} damaged row(s) appeared in the store; stopping")
            for f in fresh:
                if f.get("id") in pending_ids:
                    if not f.get("summarized"):
                        # hidden meanwhile (e.g. the nightly run found it a
                        # duplicate): a summary would contradict that
                        print(f"  {f['id']}: no longer summarised, new summary not written")
                        continue
                    for k in SUMMARY_FIELDS + ("analysis_resummarized_at",):
                        if k in by_id[f["id"]]:
                            f[k] = by_id[f["id"]][k]
            jsonl_store._rewrite_locked(aa.DATA_FILE, fresh, None)
        pending_ids.clear()

    try:
        rc = _loop(todo, keys, report, old, outcomes, pending_ids, flush, started)
    finally:
        # Ctrl-C or any error: up to SAVE_EVERY summaries are paid for and
        # already reported as replaced; write them before going.
        flush()
    if rc:
        return rc
    replaced = sum(1 for o in outcomes.values() if o == "replaced")
    print(f"done in {time.time() - started:.0f}s: {replaced} replaced, {len(outcomes) - replaced} kept")
    return 0


def _loop(todo, keys, report, old, outcomes, pending_ids, flush, started) -> int:
    for i, row in enumerate(todo, 1):
        text = aa._resolve_content_path(row).read_text(encoding="utf-8")
        before = {k: row.get(k) for k in SUMMARY_FIELDS}
        try:
            result = aa._analyze_with_fallback(text, keys, title=row.get("title", ""), source=row.get("source_id", ""),
                                               date=row.get("date", ""), metadata_only=False, article_id=row["id"])
        except aa.FatalAPIError as exc:
            # quota/auth: every later call fails the same way; what was done
            # is written by the caller
            print(f"STOPPED: {exc} -- replaced so far are saved; rerun to continue")
            return 2
        outcome = apply_result(row, result)
        outcomes[row["id"]] = outcome
        with report.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": row["id"], "source_id": row.get("source_id"), "chars": len(text),
                                "outcome": outcome}, ensure_ascii=False) + "\n")
        if outcome == "replaced":
            with old.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": row["id"], **before}, ensure_ascii=False) + "\n")
            pending_ids.append(row["id"])
        if len(pending_ids) >= SAVE_EVERY:
            flush()
        print(f"  {i}/{len(todo)} {row.get('source_id')} {outcome[:60]}  ({time.time() - started:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
