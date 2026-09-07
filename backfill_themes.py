#!/usr/bin/env python3
"""Backfill themes onto articles summarised before the prompt fix.

611 of 1331 summarised articles carry ``themes: []``.  Neither prompt ever
showed the model the 15-item allowlist its answer would be filtered against, so
it invented labels and every one of them was dropped (fixed 2026-09-07 in
``c061639``).  ``publish.py`` files an article under ``themes[0]`` and drops the
rest into a "General" bucket, so those 611 were missing from the site's theme
navigation entirely.

Only the classification is missing -- the summaries themselves are fine -- so
this re-derives themes from the stored summary and writes back nothing else.
Rewriting 611 published summaries would be a much larger change than the defect
warrants, and it would be far harder to review.

Classifying from the stored summary rather than re-reading the article costs
~300 input tokens instead of ~1,700; the summary is a faithful condensation of
the piece, which is all a 15-way classification needs.

Usage:
    python3 backfill_themes.py --dry-run          # show what would change
    python3 backfill_themes.py --limit 20         # do 20, then stop
    python3 backfill_themes.py                    # all of them
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import analyze_articles as aa

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
BJT = aa.BJT

# Reuses the analyzer's instruction rather than restating it: a second copy of
# the allowlist drifts the moment VALID_THEMES changes, and drifts silently.
BACKFILL_PROMPT = """You are a senior investment analyst. Classify a hedge fund research article by theme, using the title and an existing summary of it.

Article title: {title}
Source: {source}
Date: {date}

Existing summary:
{summary}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{"themes": [...]}}""" + aa._THEME_INSTRUCTION

MODEL = aa.MODEL_CHAIN[0]

# Write back every N classifications instead of only at the end.  With 611
# articles a crash at the last one used to discard every already-paid-for
# result; now it costs at most one batch.
CHECKPOINT_EVERY = 25


def select_articles(rows: list[dict]) -> list[dict]:
    """Summarised articles that have no themes and something to classify from."""
    return [a for a in rows
            if a.get("summarized") and not a.get("themes") and (a.get("summary_en") or "").strip()]


def _call_model(prompt: str, api_keys: dict, article_id: str = "") -> tuple[str, dict, str]:
    """One classification call. Split out so tests can stub the network.

    Returns the raw text alongside its usage so the caller can book the call
    with the outcome it actually had.  This used to log `parsed=True` here, as
    a literal, before anything had been parsed -- an unparseable reply was
    recorded as a successful classification, and the field could only ever say
    "fine".
    """
    key_name = "OPENAI_API_KEY" if MODEL in aa.OPENAI_MODELS else "GEMINI_API_KEY"
    caller = aa._call_openai if MODEL in aa.OPENAI_MODELS else aa._call_gemini
    kwargs = {"model": MODEL} if MODEL in aa.OPENAI_MODELS else {}
    return caller(prompt, api_keys[key_name], **kwargs)


def classify(article: dict, api_keys: dict) -> list[str]:
    """Return allowlisted themes for one article, or [] when nothing usable."""
    prompt = BACKFILL_PROMPT.format(
        title=article.get("title", ""), source=article.get("source_id", ""),
        date=article.get("date", ""), summary=article.get("summary_en", ""))
    # The parse lives inside the try with the call.  It used to sit outside, so
    # one chatty reply ("Sure! Here is the JSON: ...") raised JSONDecodeError
    # and aborted the whole run -- discarding every classification already paid
    # for, with no resume point.  A bad reply must cost one article.
    aid = article.get("id", "")
    usage, used, themes, ok = {}, MODEL, [], False
    try:
        raw, usage, used = _call_model(prompt, api_keys, aid)
    except Exception as e:                      # network or quota: nothing was billed
        print(f"  ! {aid}: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    try:
        payload = json.loads(aa.strip_code_fences(raw))
        themes = payload.get("themes", []) if isinstance(payload, dict) else []
        ok = True
    except Exception as e:                      # tokens were spent either way
        print(f"  ! {aid}: {type(e).__name__}: {e}", file=sys.stderr)
    aa._append_usage_log(aid, used, usage, parsed=ok)
    if not ok:
        return []
    # Round-trips through the analyzer's own parser so the allowlist filter and
    # its fuzzy matching stay in one place.
    parsed = aa._parse_llm_output(json.dumps({
        "summary_en": "-", "summary_zh": "-", "key_takeaway_en": "-",
        "key_takeaway_zh": "-", "themes": themes,
    }))
    return (parsed or {}).get("themes", [])


def apply(article: dict, api_keys: dict) -> dict:
    """Set themes on one article. An empty classification changes nothing."""
    themes = classify(article, api_keys)
    if themes:
        article["themes"] = themes
    return article


def _corpus_fingerprint() -> tuple[int, int]:
    st = DATA_FILE.stat()
    return st.st_size, st.st_mtime_ns


def _flush(rows: list[dict], backed_up: bool, fingerprint: tuple[int, int] | None = None) -> bool:
    """Persist `rows` atomically, taking one backup before the first write."""
    if fingerprint is not None and _corpus_fingerprint() != fingerprint:
        raise SystemExit(
            f"{DATA_FILE} changed underneath this run (another writer, most "
            "likely the nightly pipeline) - refusing to overwrite it")
    if not backed_up:
        stamp = datetime.now(BJT).strftime("%Y%m%d-%H%M%S")
        backup = DATA_FILE.with_name(f"{DATA_FILE.name}.bak-{stamp}-themes")
        shutil.copy2(DATA_FILE, backup)
        print(f"backup: {backup}")
    aa.save_articles(rows, path=DATA_FILE)
    return True


def run(dry_run: bool, limit: int | None, api_keys: dict) -> int:
    # Read the corpus and remember its identity. A full run takes ~40 minutes
    # while the 03:45 BJT pipeline rewrites the same file through
    # save_articles(); neither side locks, so whoever writes second silently
    # discards the other's work. Refusing is not a lock, but it turns a silent
    # lost update into a visible failure with the classifications still on disk
    # from the last checkpoint.
    fingerprint = _corpus_fingerprint()
    rows = [json.loads(l) for l in DATA_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = select_articles(rows)
    if limit is not None:
        if limit < 0:
            raise SystemExit("--limit must not be negative")
        todo = todo[:limit]           # `if limit:` treated 0 as "no limit"
    print(f"{len(todo)} article(s) to classify (model={MODEL}, dry_run={dry_run})")
    changed = unsaved = 0
    backed_up = False
    try:
        for a in todo:
            # Goes through apply() rather than repeating its logic: the
            # write-back guarantees are tested against apply(), and run() used
            # to reimplement them inline, so those tests covered a function
            # production never called.
            before = list(a.get("themes") or [])
            apply(a, api_keys)
            themes = a.get("themes") or []
            print(f"  {a['id']}  {(a.get('title') or '')[:52]:54} -> {themes or '(none)'}")
            if themes != before and not dry_run:
                changed += 1
                unsaved += 1
                if unsaved >= CHECKPOINT_EVERY:
                    backed_up = _flush(rows, backed_up, fingerprint)
                    fingerprint = _corpus_fingerprint()
                    unsaved = 0
    finally:
        # Also runs on Ctrl-C or a crash: whatever was classified is kept.
        if unsaved and not dry_run:
            backed_up = _flush(rows, backed_up, fingerprint)
    if dry_run:
        print("dry run: nothing written")
        return 0
    print(f"themes written for {changed} article(s)")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill themes on already-summarised articles")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    run(args.dry_run, args.limit, aa._load_api_keys())


if __name__ == "__main__":
    main()
