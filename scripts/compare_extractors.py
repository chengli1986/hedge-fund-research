#!/usr/bin/env python3
"""Re-extract stored articles and require the text to be identical.

The safety rope for changing a content extractor. Stage 1 has
scripts/compare_fetchers.py -- a listing moves to a template only when both
return the same titles, urls and dates from the live site. Bodies have had
no equivalent, which is how the 2026-09-26 template switch dropped two
fields ARK's fallback reads without anything noticing.

This reuses the weekly audit's fetch-into-a-temp-directory helper
(scripts/content_audit.py), so production content/ is never written, and
adds the one thing that audit deliberately does not do: compare character
for character. The audit's verdict is fuzzy on purpose -- it drops
punctuation and measures shingle overlap, so a site's own edit does not
read as our regression. Proving a refactor changed nothing needs the strict
answer instead.

    python3 scripts/compare_extractors.py --source robeco
    python3 scripts/compare_extractors.py --all --per-source 3
    python3 scripts/compare_extractors.py --all --json

Exit 0 when every compared body is identical, 1 on any difference or failed
fetch, 2 when nothing was compared -- an empty run must not read as a pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_spec = importlib.util.spec_from_file_location(
    "content_audit", BASE_DIR / "scripts" / "content_audit.py")
_audit = importlib.util.module_from_spec(_spec)
sys.modules["content_audit"] = _audit
_spec.loader.exec_module(_audit)

_fetch_to_temp = _audit._fetch_to_temp
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
CONTENT_DIR = BASE_DIR / "content"
# Live sites, one article after another: the weekly audit waits between
# fetches for the same reason.
PAUSE_SECONDS = 1.0


def _stored_text(article: dict, stored_dir: Path) -> str | None:
    path = article.get("content_path")
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = BASE_DIR / path
    if not candidate.exists():
        candidate = stored_dir / f"{article['id']}.txt"
    if not candidate.exists():
        return None
    return candidate.read_text(encoding="utf-8", errors="ignore")


def _first_difference(a: str, b: str) -> dict | None:
    """Where the two texts part company, with a little context each side."""
    if a == b:
        return None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return {"at": i, "stored": a[max(0, i - 40):i + 40],
                    "new": b[max(0, i - 40):i + 40]}
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return {"at": len(shorter), "stored": a[-80:], "new": b[-80:],
            "note": "one text is a prefix of the other"}


def compare_article(article: dict, fetcher, stored_dir: Path | None = None) -> dict:
    """Re-extract one article and compare it with what is on disk."""
    stored_dir = stored_dir or CONTENT_DIR
    out = {"id": article.get("id"), "source_id": article.get("source_id"),
           "url": article.get("url"), "title": (article.get("title") or "")[:60],
           "same": False, "failed": False, "skipped": None,
           "stored_chars": 0, "new_chars": 0, "first_diff": None}

    stored = _stored_text(article, stored_dir)
    if stored is None:
        out["skipped"] = "no stored body"
        return out

    result, _evidence, new = _fetch_to_temp(article, fetcher)
    out["stored_chars"], out["new_chars"] = len(stored), len(new or "")
    if not result or not result[0]:
        out["failed"] = True
        return out

    out["same"] = stored == new
    if not out["same"]:
        out["first_diff"] = _first_difference(stored, new or "")
    return out


def verdict(results: list[dict]) -> int:
    """0 identical, 1 a difference or a failure, 2 nothing compared."""
    compared = [r for r in results if not r.get("skipped")]
    if not compared:
        return 2
    return 0 if all(r.get("same") for r in compared) else 1


def _load_rows() -> list[dict]:
    return [json.loads(line) for line in DATA_FILE.read_text().splitlines() if line.strip()]


def _configured() -> list[str]:
    cfg = json.loads((BASE_DIR / "config" / "sources.json").read_text())
    return [s["id"] for s in cfg["sources"]]


def _pick(rows: list[dict], configured: list[str], sources: list[str] | None,
          per_source: int) -> list[dict]:
    """The weekly audit's sampling: newest first, no reused URLs, no declines."""
    chosen = _audit.sample_articles(rows, configured, per_source=per_source,
                                    content_dir=CONTENT_DIR)
    if sources:
        chosen = [a for a in chosen if a.get("source_id") in set(sources)]
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-extract stored articles, require identical text")
    parser.add_argument("--source", action="append", default=[],
                        help="compare this source (repeatable)")
    parser.add_argument("--all", action="store_true", help="compare every configured source")
    parser.add_argument("--per-source", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pause", type=float, default=PAUSE_SECONDS)
    args = parser.parse_args(argv)
    if not args.source and not args.all:
        parser.error("give --source ID (repeatable) or --all")

    import fetch_content as fc

    rows = _load_rows()
    articles = _pick(rows, _configured(), args.source or None, args.per_source)

    results = []
    for i, article in enumerate(articles):
        try:
            fetcher = fc.content_fetcher_for(article)
        except KeyError:
            results.append({"id": article.get("id"), "source_id": article.get("source_id"),
                            "skipped": "no content fetcher", "same": False})
            continue
        results.append(compare_article(article, fetcher))
        if i + 1 < len(articles):
            time.sleep(args.pause)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        by_source: dict[str, list] = {}
        for r in results:
            by_source.setdefault(r.get("source_id") or "?", []).append(r)
        for sid, rs in sorted(by_source.items()):
            same = sum(1 for r in rs if r.get("same"))
            skipped = sum(1 for r in rs if r.get("skipped"))
            failed = sum(1 for r in rs if r.get("failed"))
            diff = len(rs) - same - skipped - failed
            mark = "✓" if diff == 0 and failed == 0 else "✗"
            print(f"  {mark} {sid:26} 逐字一致 {same}/{len(rs) - skipped}"
                  + (f" · 不一致 {diff}" if diff else "")
                  + (f" · 抓取失败 {failed}" if failed else "")
                  + (f" · 跳过 {skipped}" if skipped else ""))
            for r in rs:
                if r.get("first_diff"):
                    d = r["first_diff"]
                    print(f"      {r['title']}  第 {d['at']} 字起不同")
                    print(f"        库里: {d['stored']!r}")
                    print(f"        现在: {d['new']!r}")
                elif r.get("failed"):
                    print(f"      {r['title']}  抓取失败（库里 {r['stored_chars']} 字）")
    return verdict(results)


if __name__ == "__main__":
    sys.exit(main())
