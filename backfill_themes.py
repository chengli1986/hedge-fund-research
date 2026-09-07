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


def select_articles(rows: list[dict]) -> list[dict]:
    """Summarised articles that have no themes and something to classify from."""
    return [a for a in rows
            if a.get("summarized") and not a.get("themes") and (a.get("summary_en") or "").strip()]


def _call_model(prompt: str, api_keys: dict, article_id: str = "") -> str:
    """One classification call. Split out so tests can stub the network.

    Books the call against the real article id, not a synthetic label: an
    anonymous row logs fine and is useless, which is the failure the accounting
    guard was built for.
    """
    key_name = "OPENAI_API_KEY" if MODEL in aa.OPENAI_MODELS else "GEMINI_API_KEY"
    caller = aa._call_openai if MODEL in aa.OPENAI_MODELS else aa._call_gemini
    kwargs = {"model": MODEL} if MODEL in aa.OPENAI_MODELS else {}
    text, usage, used = caller(prompt, api_keys[key_name], **kwargs)
    aa._append_usage_log(article_id, used, usage, parsed=True)
    return text


def classify(article: dict, api_keys: dict) -> list[str]:
    """Return allowlisted themes for one article, or [] when nothing usable."""
    prompt = BACKFILL_PROMPT.format(
        title=article.get("title", ""), source=article.get("source_id", ""),
        date=article.get("date", ""), summary=article.get("summary_en", ""))
    try:
        raw = _call_model(prompt, api_keys, article.get("id", ""))
    except Exception as e:                      # network/quota: leave for a later run
        print(f"  ! {article.get('id')}: {e}", file=sys.stderr)
        return []
    # Round-trips through the analyzer's own parser so the allowlist filter and
    # its fuzzy matching stay in one place.
    parsed = aa._parse_llm_output(json.dumps({
        "summary_en": "-", "summary_zh": "-", "key_takeaway_en": "-",
        "key_takeaway_zh": "-", "themes": (json.loads(_strip_fences(raw)) or {}).get("themes", []),
    }))
    return (parsed or {}).get("themes", [])


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return t.strip() or "{}"


def apply(article: dict, api_keys: dict) -> dict:
    """Set themes on one article. An empty classification changes nothing."""
    themes = classify(article, api_keys)
    if themes:
        article["themes"] = themes
    return article


def run(dry_run: bool, limit: int | None, api_keys: dict) -> int:
    rows = [json.loads(l) for l in DATA_FILE.read_text().splitlines() if l.strip()]
    todo = select_articles(rows)
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} article(s) to classify (model={MODEL}, dry_run={dry_run})")
    changed = 0
    for a in todo:
        themes = classify(a, api_keys)
        print(f"  {a['id']}  {(a.get('title') or '')[:52]:54} -> {themes or '(none)'}")
        if themes and not dry_run:
            a["themes"] = themes
            changed += 1
    if dry_run:
        print("dry run: nothing written")
        return 0
    if changed:
        stamp = datetime.now(BJT).strftime("%Y%m%d-%H%M%S")
        backup = DATA_FILE.with_name(f"{DATA_FILE.name}.bak-{stamp}-themes")
        shutil.copy2(DATA_FILE, backup)
        print(f"backup: {backup}")
        DATA_FILE.write_text("".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
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
