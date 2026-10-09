#!/usr/bin/env python3
"""Tag every summarised article with the new 41-tag taxonomy, from its full text.

One-off for the 2026-10 reclassification (docs/tag-taxonomy.md, Q6), kept for
the next time the taxonomy changes. Writes ONLY these fields:

    tags        list of tag ids, article type first (taxonomy.flatten)
    tags_model  the model that produced them
    tags_at     when (BJT)
    content_path  filled in only where it is empty and content/<id>.txt exists
                  (10 metlife rows whose PDF body was backfilled 2026-09-15
                  without it; _resolve_content_path already falls back to the
                  id, so this changes nothing the pipeline reads)

Summaries and the old `themes` field are not touched: the live page keeps
reading `themes` until the page and the nightly run switch over together.

Safety:
- an answer is stored only if taxonomy.validate() accepts it; an invalid one
  is retried, then the article is left untagged and listed in the report;
- each asset/topic/method tag must come with a passage taxonomy.check_evidence()
  finds in the document, else that tag is dropped (named in the report, which
  also keeps the passages; the store gets only the tags);
- a quota/billing or auth error stops the whole run at once -- every later
  call would fail the same way (2026-10-07: the account ran out of credit
  mid-pilot and every following call returned 429 credit_balance_exhausted);
- already-tagged articles are skipped, so a stopped run resumes where it left off;
  --retag-before T re-tags those tagged before T (after a definition change;
  2026-10-08 round 4), and resumes the same way when rerun with the same T;
- the store is copied to --backup before the first write, and writes go through
  jsonl_store under its lock, SAVE_EVERY articles at a time, re-reading the
  store inside the lock so work written meanwhile by another job is kept.

    python3 scripts/tag_articles.py --dry-run
    python3 scripts/tag_articles.py --limit 100 --backup DIR
    python3 scripts/tag_articles.py --backup DIR
    python3 scripts/tag_articles.py --backup DIR --retag-before 2026-10-08T12:00:00+08:00
    python3 scripts/tag_articles.py --nightly      # run_pipeline.sh Stage 3b (since 2026-10-08)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import analyze_articles as aa  # noqa: E402
import jsonl_store  # noqa: E402
import taxonomy  # noqa: E402

MODEL = aa.MODEL_CHAIN[0]
TAG_FIELDS = ("tags", "tags_model", "tags_at")
SAVE_EVERY = 25
ATTEMPTS = 3
# Nights an article may fail before it is no longer asked (stage-3 audit N1):
# an answer that breaks the rules three nights running will break them on the
# fourth. The night it is given up the run exits GAVE_UP_RC, which
# run_pipeline.sh alerts on, so a person looks at it once; after that it is
# skipped quietly. Clearing `tag_failures` puts it back in the queue.
MAX_TAG_NIGHTS = 3
GAVE_UP_RC = 3
# Anything that is not about one article -- a damaged store, a crash outside the
# per-article loop. Exit 1 means "some articles untagged, retried tomorrow" and
# run_pipeline.sh only logs it; an uncaught traceback also exits 1, so these
# used to read as that harmless case (stage-3 audit N2).
BROKEN_RC = 4
# Quota/auth detection is shared with Stage 3 (analyze_articles).
FATAL_CODES = aa.FATAL_CODES
FatalAPIError = aa.FatalAPIError
_error_code = aa._error_code
is_fatal = aa.is_fatal

RETRY_NOTE = ("\n\nYour previous answer was rejected: {errors}. Answer again with the same JSON "
              "shape, fixing exactly that and keeping to every limit above.\n")


def candidates(rows: list[dict], retag_before: str | None = None,
               only=None) -> list[dict]:
    """Summarised, with a readable body, and not yet tagged -- or, with retag_before,
    tagged before that time (so a stopped re-tag resumes with the same value).
    With `only` (a predicate on the row), just the rows it accepts."""
    out = []
    for r in rows:
        if not r.get("summarized"):
            continue
        if only is not None and not only(r):
            continue
        if r.get("tags") and not (retag_before and (r.get("tags_at") or "") < retag_before):
            continue
        if not r.get("tags") and int(r.get("tag_failures") or 0) >= MAX_TAG_NIGHTS:
            continue
        try:
            if aa._resolve_content_path(r).is_file():
                out.append(r)
        except ValueError:
            continue
    return out


def build_prompt(row: dict, text: str, series: str = "") -> str:
    return ("You are a senior investment analyst classifying a research article.\n\n"
            f"Title: {aa.fence_safe(row.get('title', ''), limit=aa.MAX_TITLE_CHARS)}\n"
            f"Source: {row.get('source_id', '')}\nDate: {row.get('date', '')}\n"
            f"{aa.fence_safe(series) + chr(10) if series else ''}\n"
            f"<<<BEGIN COPIED DOCUMENT>>>\n{aa.fence_safe(text[:aa.MAX_CONTENT_CHARS])}\n<<<END COPIED DOCUMENT>>>\n\n"
            f"{taxonomy.instruction()}")


def classify(row: dict, api_key: str, call=aa._call_openai, sleep=time.sleep,
             stop: threading.Event | None = None,
             series: str = "") -> tuple[list[str] | None, str, list[tuple[dict, bool]], dict]:
    """Tag one article. Returns (tags or None, outcome, [(usage, parsed_ok) per billed call],
    evidence {tag: passage} for the tags kept).

    Tags whose evidence passage is not in the document are dropped and named in
    the outcome. Raises FatalAPIError on quota/billing/auth errors."""
    text = aa._resolve_content_path(row).read_text(encoding="utf-8")
    prompt = build_prompt(row, text, series)
    billed: list[tuple[dict, bool]] = []
    last = "no attempt"
    for attempt in range(ATTEMPTS):
        if stop is not None and stop.is_set():
            return None, "skipped: run stopped", billed, {}
        try:
            raw, usage, _model = call(prompt + (RETRY_NOTE.format(errors=last[len("invalid: "):])
                                                if last.startswith("invalid: ") else ""),
                                      api_key, model=MODEL)
        except aa.EmptyAnswer as exc:
            # A refusal: billed, nothing to read. Uncaught, it ended the whole
            # run (audit N2).
            billed.append((exc.usage, False))
            last = f"no answer: {exc}"[:200]
            continue
        except requests.HTTPError as exc:
            if is_fatal(exc):
                raise FatalAPIError(f"{exc.response.status_code} {_error_code(exc) or exc}", billed=billed) from exc
            last = f"HTTP {exc.response.status_code if exc.response is not None else '?'}"
            sleep(5 * (attempt + 1))
            continue
        except requests.RequestException as exc:
            last = type(exc).__name__
            sleep(5 * (attempt + 1))
            continue
        try:
            answer = json.loads(aa.strip_code_fences(raw))
            errs = taxonomy.validate(answer)
        except ValueError as exc:
            errs = [f"not JSON: {exc}"]
        billed.append((usage, not errs))
        if not errs:
            kept, dropped = taxonomy.check_evidence(answer, text[:aa.MAX_CONTENT_CHARS])
            evidence = {t: answer["evidence"][t] for g in taxonomy.EVIDENCE_GROUPS for t in kept[g]}
            outcome = "tagged" + (f" (dropped {'; '.join(dropped)})" if dropped else "")
            return taxonomy.flatten(kept), outcome, billed, evidence
        last = "invalid: " + "; ".join(errs)
    return None, f"failed after {ATTEMPTS} attempts: {last}", billed, {}


def apply_tags(row: dict, tags: list[str]) -> None:
    row["tags"] = tags
    row["tags_model"] = MODEL
    row["tags_at"] = datetime.now(aa.BJT).isoformat(timespec="seconds")
    if not row.get("content_path"):
        p = aa.CONTENT_DIR / f"{row['id']}.txt"
        if p.is_file():
            row["content_path"] = str(p.relative_to(aa.BASE_DIR))


def flush(path: Path, done: dict[str, dict], failed: set[str] | None = None,
          gave_up: list[str] | None = None) -> int:
    """Write the tag fields of `done` onto the store as it is now. Returns rows written.

    Rows in `failed` get their `tag_failures` night count raised; ids that reach
    MAX_TAG_NIGHTS are appended to `gave_up`. A tagged row's count is cleared."""
    failed = failed if failed is not None else set()
    if not done and not failed:
        return 0
    written = 0
    with jsonl_store.file_lock(path):
        fresh, bad = jsonl_store._read_locked(path)
        if bad:
            raise RuntimeError(f"{bad} damaged row(s) in the store; stopping without writing")
        for f in fresh:
            if f.get("id") in failed:
                f["tag_failures"] = int(f.get("tag_failures") or 0) + 1
                if f["tag_failures"] == MAX_TAG_NIGHTS and gave_up is not None:
                    gave_up.append(f["id"])
                continue
            src = done.get(f.get("id"))
            if src is None:
                continue
            f.pop("tag_failures", None)
            for k in TAG_FIELDS:
                f[k] = src[k]
            if not f.get("content_path") and src.get("content_path"):
                f["content_path"] = src["content_path"]
            written += 1
        jsonl_store._rewrite_locked(path, fresh, None)
    done.clear()
    failed.clear()
    return written


def run(path: Path, api_key: str, backup: Path | None, limit: int = 0, workers: int = 3,
        dry_run: bool = False, retag_before: str | None = None, only_series: bool = False,
        also_types: tuple[str, ...] = (), fresh_snapshot: bool = False, call=aa._call_openai, sleep=time.sleep, log_usage=aa._append_usage_log) -> int:
    rows, damaged = jsonl_store.read_rows(path)
    if damaged:
        print(f"{damaged} damaged row(s) in the store; refusing to rewrite it")
        return BROKEN_RC
    index = taxonomy.series_index([r for r in rows if r.get("summarized")])
    only = (lambda r: bool(taxonomy.series_note(r, index)) or (r.get("tags") or [None])[0] in also_types) \
        if only_series else None
    todo = candidates(rows, retag_before, only)
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} article(s) to tag (model={MODEL}, workers={workers})", flush=True)
    if dry_run or not todo:
        return 0
    if backup is None:
        raise SystemExit("--backup DIR is required for a real run")
    backup.mkdir(parents=True, exist_ok=True)
    snapshot = backup / "articles.jsonl.before"
    if fresh_snapshot or not snapshot.exists():
        shutil.copy2(path, snapshot)
    report = backup / "report.jsonl"

    stop = threading.Event()
    done: dict[str, dict] = {}
    failed: set[str] = set()
    gave_up: list[str] = []
    counts = {"tagged": 0, "failed": 0, "skipped": 0}
    tokens = [0, 0]
    fatal: str | None = None
    started = time.time()

    def work(row):
        try:
            return row, *classify(row, api_key, call=call, sleep=sleep, stop=stop,
                                  series=taxonomy.series_note(row, index))
        except FatalAPIError as exc:
            stop.set()
            return row, None, f"fatal: {exc}", exc.billed, {}
        except Exception as exc:
            # One article's surprise must not take the others down with it:
            # fut.result() re-raised it in the main loop, unsaved work was lost,
            # and the traceback's exit 1 read as "some left untagged" (N2).
            return row, None, f"failed: {type(exc).__name__}: {exc}"[:300], [], {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, r) for r in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            row, tags, outcome, billed, evidence = fut.result()
            for usage, ok in billed:
                log_usage(row["id"], MODEL, usage, parsed=ok)
                tokens[0] += usage.get("prompt_tokens") or 0
                tokens[1] += usage.get("completion_tokens") or 0
            if outcome.startswith("fatal"):
                fatal = fatal or outcome
            if tags is not None:
                apply_tags(row, tags)
                done[row["id"]] = row
                counts["tagged"] += 1
            elif outcome.startswith(("skipped", "fatal")):
                counts["skipped"] += 1
            else:
                counts["failed"] += 1
                failed.add(row["id"])
            with report.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": row["id"], "source_id": row.get("source_id"),
                                    "outcome": outcome, "tags": tags, "evidence": evidence},
                                   ensure_ascii=False) + "\n")
            if len(done) >= SAVE_EVERY:
                flush(path, done, failed, gave_up)
            print(f"  {i}/{len(todo)} {row.get('source_id', '')[:18]:18} {outcome[:50]:50} "
                  f"in={tokens[0]:,} out={tokens[1]:,} ({time.time() - started:.0f}s)", flush=True)
    flush(path, done, failed, gave_up)
    print(f"done in {time.time() - started:.0f}s: {counts['tagged']} tagged, {counts['failed']} failed, "
          f"{counts['skipped']} skipped; tokens in={tokens[0]:,} out={tokens[1]:,}")
    if fatal:
        print(f"STOPPED: {fatal} -- fix it and run again; tagged articles are kept and will be skipped")
        return 2
    if gave_up:
        print(f"GAVE UP after {MAX_TAG_NIGHTS} failed nights (no longer asked; clear tag_failures "
              f"to retry): {', '.join(gave_up)}")
        return GAVE_UP_RC
    return 1 if counts["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--backup", type=Path, help="directory for the store snapshot and the report")
    ap.add_argument("--nightly", action="store_true",
                    help="run_pipeline.sh Stage 3b: tag what is untagged, backup in logs/tag-nightly "
                         "with the snapshot retaken every run")
    ap.add_argument("--only-series", action="store_true",
                    help="only articles in a column-like group (taxonomy.series_index)")
    ap.add_argument("--also-type", action="append", default=[], metavar="TYPE",
                    help="with --only-series, also articles whose current type is TYPE")
    ap.add_argument("--retag-before", metavar="ISO_TIME",
                    help="also re-tag articles whose tags_at is earlier than this (BJT ISO, e.g. "
                         "2026-10-08T12:00:00+08:00); rerun with the same value to resume")
    args = ap.parse_args(argv)
    if args.nightly:
        args.backup = aa.BASE_DIR / "logs" / "tag-nightly"
    key = "" if args.dry_run else aa._load_api_keys()["OPENAI_API_KEY"]
    return run(aa.DATA_FILE, key, args.backup, limit=args.limit, workers=args.workers,
               dry_run=args.dry_run, retag_before=args.retag_before, only_series=args.only_series,
               also_types=tuple(args.also_type), fresh_snapshot=args.nightly)


def entry() -> int:
    """main() with any uncaught error turned into BROKEN_RC, not the traceback's 1."""
    try:
        return main()
    except Exception:
        import traceback
        traceback.print_exc()
        return BROKEN_RC


if __name__ == "__main__":
    sys.exit(entry())
