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
# Error codes OpenAI returns when retrying cannot help.
FATAL_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached",
               "invalid_api_key", "account_deactivated"}


class FatalAPIError(Exception):
    """Every later call would fail the same way: stop the run."""
    def __init__(self, message, billed=None):
        super().__init__(message)
        self.billed: list[tuple[dict, bool]] = billed if billed is not None else []


def _error_code(exc: requests.HTTPError) -> str:
    try:
        err = exc.response.json().get("error") or {}
        return str(err.get("code") or err.get("type") or "")
    except Exception:
        return ""


def is_fatal(exc: Exception) -> bool:
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    if exc.response.status_code in (401, 403):
        return True
    return exc.response.status_code == 429 and _error_code(exc) in FATAL_CODES


def candidates(rows: list[dict], retag_before: str | None = None) -> list[dict]:
    """Summarised, with a readable body, and not yet tagged -- or, with retag_before,
    tagged before that time (so a stopped re-tag resumes with the same value)."""
    out = []
    for r in rows:
        if not r.get("summarized"):
            continue
        if r.get("tags") and not (retag_before and (r.get("tags_at") or "") < retag_before):
            continue
        try:
            if aa._resolve_content_path(r).is_file():
                out.append(r)
        except ValueError:
            continue
    return out


def build_prompt(row: dict, text: str) -> str:
    return ("You are a senior investment analyst classifying a research article.\n\n"
            f"Title: {aa.fence_safe(row.get('title', ''), limit=aa.MAX_TITLE_CHARS)}\n"
            f"Source: {row.get('source_id', '')}\nDate: {row.get('date', '')}\n\n"
            f"<<<BEGIN COPIED DOCUMENT>>>\n{aa.fence_safe(text[:aa.MAX_CONTENT_CHARS])}\n<<<END COPIED DOCUMENT>>>\n\n"
            f"{taxonomy.instruction()}")


def classify(row: dict, api_key: str, call=aa._call_openai, sleep=time.sleep,
             stop: threading.Event | None = None) -> tuple[list[str] | None, str, list[tuple[dict, bool]], dict]:
    """Tag one article. Returns (tags or None, outcome, [(usage, parsed_ok) per billed call],
    evidence {tag: passage} for the tags kept).

    Tags whose evidence passage is not in the document are dropped and named in
    the outcome. Raises FatalAPIError on quota/billing/auth errors."""
    text = aa._resolve_content_path(row).read_text(encoding="utf-8")
    prompt = build_prompt(row, text)
    billed: list[tuple[dict, bool]] = []
    last = "no attempt"
    for attempt in range(ATTEMPTS):
        if stop is not None and stop.is_set():
            return None, "skipped: run stopped", billed, {}
        try:
            raw, usage, _model = call(prompt, api_key, model=MODEL)
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


def flush(path: Path, done: dict[str, dict]) -> int:
    """Write the tag fields of `done` onto the store as it is now. Returns rows written."""
    if not done:
        return 0
    written = 0
    with jsonl_store.file_lock(path):
        fresh, bad = jsonl_store._read_locked(path)
        if bad:
            raise SystemExit(f"{bad} damaged row(s) in the store; stopping without writing")
        for f in fresh:
            src = done.get(f.get("id"))
            if src is None:
                continue
            for k in TAG_FIELDS:
                f[k] = src[k]
            if not f.get("content_path") and src.get("content_path"):
                f["content_path"] = src["content_path"]
            written += 1
        jsonl_store._rewrite_locked(path, fresh, None)
    done.clear()
    return written


def run(path: Path, api_key: str, backup: Path | None, limit: int = 0, workers: int = 3,
        dry_run: bool = False, retag_before: str | None = None, call=aa._call_openai, sleep=time.sleep, log_usage=aa._append_usage_log) -> int:
    rows, damaged = jsonl_store.read_rows(path)
    if damaged:
        print(f"{damaged} damaged row(s) in the store; refusing to rewrite it")
        return 1
    todo = candidates(rows, retag_before)
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} article(s) to tag (model={MODEL}, workers={workers})", flush=True)
    if dry_run or not todo:
        return 0
    if backup is None:
        raise SystemExit("--backup DIR is required for a real run")
    backup.mkdir(parents=True, exist_ok=True)
    snapshot = backup / "articles.jsonl.before"
    if not snapshot.exists():
        shutil.copy2(path, snapshot)
    report = backup / "report.jsonl"

    stop = threading.Event()
    done: dict[str, dict] = {}
    counts = {"tagged": 0, "failed": 0, "skipped": 0}
    tokens = [0, 0]
    fatal: str | None = None
    started = time.time()

    def work(row):
        try:
            return row, *classify(row, api_key, call=call, sleep=sleep, stop=stop)
        except FatalAPIError as exc:
            stop.set()
            return row, None, f"fatal: {exc}", exc.billed, {}

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
            with report.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": row["id"], "source_id": row.get("source_id"),
                                    "outcome": outcome, "tags": tags, "evidence": evidence},
                                   ensure_ascii=False) + "\n")
            if len(done) >= SAVE_EVERY:
                flush(path, done)
            print(f"  {i}/{len(todo)} {row.get('source_id', '')[:18]:18} {outcome[:50]:50} "
                  f"in={tokens[0]:,} out={tokens[1]:,} ({time.time() - started:.0f}s)", flush=True)
    flush(path, done)
    print(f"done in {time.time() - started:.0f}s: {counts['tagged']} tagged, {counts['failed']} failed, "
          f"{counts['skipped']} skipped; tokens in={tokens[0]:,} out={tokens[1]:,}")
    if fatal:
        print(f"STOPPED: {fatal} -- fix it and run again; tagged articles are kept and will be skipped")
        return 2
    return 1 if counts["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--backup", type=Path, help="directory for the store snapshot and the report")
    ap.add_argument("--retag-before", metavar="ISO_TIME",
                    help="also re-tag articles whose tags_at is earlier than this (BJT ISO, e.g. "
                         "2026-10-08T12:00:00+08:00); rerun with the same value to resume")
    args = ap.parse_args(argv)
    key = "" if args.dry_run else aa._load_api_keys()["OPENAI_API_KEY"]
    return run(aa.DATA_FILE, key, args.backup, limit=args.limit, workers=args.workers,
               dry_run=args.dry_run, retag_before=args.retag_before)


if __name__ == "__main__":
    sys.exit(main())
