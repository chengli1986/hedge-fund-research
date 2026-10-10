#!/usr/bin/env python3
"""Give month-only listing dates the real publication day from the article page.

Many listings show only "October 2026". parse_date stores that as the month's
LAST day so staleness checks don't fire early, which dates the piece into the
future for the rest of the month (2026-10-08: 9 articles sat at 10-31 at the
top of the page) and hides when it really appeared. The article page itself
usually says: of 12 sampled pieces from the 7 affected sources, 10 carried
the day in their metadata and Man Group's carries it in the URL
("views-from-the-floor-2026-6-oct").

For every row whose date_raw is month-only and that has no date_basis yet:

    page        a datePublished / article:published_time / <time> on the page
    url         a day in the URL
    first_seen  the page has none: the day we first fetched it, if in that month
    month_end   none, and first fetched after the month: keep the month end

Only a day inside the month the listing named is taken (a sidebar or a
"related articles" date is not this article's). `date` gets the result,
`date_listed` keeps what was there, and `date_raw` is never touched:
fetch_articles reads it to treat the listing's next "October 2026" as the same
article re-normalised rather than a new issue at the same URL. A page that
cannot be fetched is retried on later runs and falls back after FETCH_TRIES.

Writes go through jsonl_store under its lock, re-reading the store inside it
and touching only the date fields, as scripts/tag_articles.py does.

    python3 scripts/refine_dates.py --dry-run
    python3 scripts/refine_dates.py            # nightly, after Stage 1
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import analyze_articles as aa  # noqa: E402
import fetch_articles as fa  # noqa: E402
import jsonl_store  # noqa: E402

DATE_FIELDS = ("date", "date_listed", "date_basis", "date_fetch_errors")
FETCH_TRIES = 3
# A page date this far after the day we first saw the article is not its
# publication date (acadian "Quick Take: The New Challenge": first seen
# 06-12, datePublished 06-28). Wellington's dates run 1-4 days after our
# first fetch, consistently; a week covers that.
MAX_DAYS_AFTER_SEEN = 7
REPORT = BASE_DIR / "logs" / "refine-dates.jsonl"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9

# In order of trust: the article's own structured date first.
# Quotes may arrive escaped inside an embedded JSON string
# (Baillie Gifford: datePublished\":\"2026-10-01\"), hence the optional backslashes.
_PAGE_PATTERNS = [
    r'"?datePublished\\?"\s*:\s*\\?"([^"\\]+)',
    r'"?publishDate\\?"\s*:\s*\\?"([^"\\]+)',
    r'<meta[^>]+property="article:published_time"[^>]+content="([^"]+)"',
    r'<meta[^>]+content="([^"]+)"[^>]+property="article:published_time"',
    r'<meta[^>]+name="(?:publish[-_]?date|publication[-_]date|date|dc\.date|citation_publication_date)"[^>]+content="([^"]+)"',
    r'<time[^>]+datetime="([^"]+)"',
]
_URL_YMD = re.compile(r"/(20\d\d)[/-](\d{1,2})[/-](\d{1,2})(?:[/?#-]|$)")
_URL_YDM = re.compile(r"-(20\d\d)-(\d{1,2})-([A-Za-z]{3,9})(?:[/?#]|$)")   # Man: ...-2026-6-oct


def _month_boundary(date: str) -> bool:
    """A stored date on the 1st or the last day of its month: what a month-only
    label was normalised to (parse_date), as opposed to a real day."""
    try:
        d = datetime.strptime(date[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return d.day == 1 or d.day == calendar.monthrange(d.year, d.month)[1]


def candidates(rows: list[dict]) -> list[dict]:
    """Month-only labels whose stored date is still the normalised month boundary.

    The label alone is not enough: Wellington shows "August 2026" but its
    <date datetime> attribute carries the day, which the fetcher stores. Taking
    those as month-only replaced 7 real days with a fallback (08-17 -> 08-31,
    stage-3 health check 2026-10-10)."""
    return [r for r in rows
            if fa._MONTH_ONLY_RE.match((r.get("date_raw") or "").strip())
            and not r.get("date_basis") and _month_boundary(r.get("date") or "")]


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _to_day(value: str) -> str | None:
    value = value.strip()
    m = re.match(r"(20\d\d)-(\d\d)-(\d\d)", value)
    if m:
        return _iso(*map(int, m.groups()))
    parsed = fa.parse_date(value)
    return parsed if parsed and not fa._MONTH_ONLY_RE.match(value) else None


def date_from_page(html: str, month: str, latest: str) -> str | None:
    for pat in _PAGE_PATTERNS:
        for value in re.findall(pat, html or "", re.I):
            day = _to_day(value)
            if day and day[:7] == month and day <= latest:
                return day
    return None


def _latest_plausible(row: dict, today: str) -> str:
    seen = (row.get("fetched_at") or "")[:10]
    try:
        cap = (datetime.fromisoformat(seen) + timedelta(days=MAX_DAYS_AFTER_SEEN)).strftime("%Y-%m-%d")
    except ValueError:
        return today
    return min(cap, today)


def date_from_url(url: str, month: str, today: str) -> str | None:
    days = [_iso(int(y), int(mo), int(d)) for y, mo, d in _URL_YMD.findall(url or "")]
    for y, d, mon in _URL_YDM.findall(url or ""):
        if mon.lower() in _MONTHS:
            days.append(_iso(int(y), _MONTHS[mon.lower()], int(d)))
    for day in days:
        if day and day[:7] == month and day <= today:
            return day
    return None


def _fallback(row: dict, month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    month_end = f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    seen = (row.get("fetched_at") or "")[:10]
    if seen[:7] == month:
        return min(seen, month_end), "first_seen"
    return month_end, "month_end"


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()
    return resp.text


def refine(row: dict, today: str, get=_get) -> dict:
    """The new date fields for one row (or only a raised error count)."""
    month = row["date"][:7]
    url = row.get("url") or ""
    latest = _latest_plausible(row, today)
    day = date_from_url(url, month, latest)
    basis = "url" if day else ""
    if not day and not url.lower().split("?")[0].endswith(".pdf"):
        try:
            day = date_from_page(get(url), month, latest)
            basis = "page" if day else ""
        except requests.HTTPError as exc:
            # 404/410: the publisher removed it (research-affiliates); retrying cannot help.
            if exc.response is None or exc.response.status_code not in (404, 410):
                errors = int(row.get("date_fetch_errors") or 0) + 1
                if errors < FETCH_TRIES:
                    return {"date_fetch_errors": errors, "_error": f"HTTPError: {exc}"[:200]}
        except Exception as exc:
            errors = int(row.get("date_fetch_errors") or 0) + 1
            if errors < FETCH_TRIES:
                return {"date_fetch_errors": errors, "_error": f"{type(exc).__name__}: {exc}"[:200]}
    if not day:
        day, basis = _fallback(row, month)
    return {"date": day, "date_listed": row["date"], "date_basis": basis}


def flush(path: Path, results: dict[str, dict]) -> int:
    if not results:
        return 0
    written = 0
    with jsonl_store.file_lock(path):
        fresh, bad = jsonl_store._read_locked(path)
        if bad:
            raise SystemExit(f"{bad} damaged row(s) in the store; stopping without writing")
        for r in fresh:
            new = results.get(r.get("id"))
            if new is None or r.get("date_basis"):
                continue
            r.update({k: v for k, v in new.items() if k in DATE_FIELDS})
            written += 1
        jsonl_store._rewrite_locked(path, fresh, None)
    return written


def run(path: Path, dry_run: bool = False, limit: int = 0, workers: int = 4, get=_get,
        report: Path | None = REPORT, today: str | None = None) -> int:
    rows, damaged = jsonl_store.read_rows(path)
    if damaged:
        print(f"{damaged} damaged row(s) in the store; refusing to rewrite it")
        return 1
    today = today or datetime.now(aa.BJT).strftime("%Y-%m-%d")
    todo = candidates(rows)[:limit or None]
    print(f"{len(todo)} month-only date(s) to refine", flush=True)
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(lambda r: (r, refine(r, today, get)), todo))
    results = {r["id"]: new for r, new in out}
    tally: dict[str, int] = {}
    for r, new in out:
        k = new.get("date_basis") or "retry later"
        tally[k] = tally.get(k, 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    if dry_run:
        for r, new in out[:20]:
            print(f"  {r['source_id']:20} {r['date']} -> {new.get('date', '-')} ({new.get('date_basis') or new.get('_error')})")
        return 0
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as f:
            for r, new in out:
                f.write(json.dumps({"at": today, "id": r["id"], "source_id": r["source_id"],
                                    "url": r.get("url"), "was": r["date"], **new}, ensure_ascii=False) + "\n")
    print(f"written: {flush(path, results)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    started = time.time()
    rc = run(aa.DATA_FILE, dry_run=args.dry_run, limit=args.limit)
    print(f"done in {time.time() - started:.0f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
