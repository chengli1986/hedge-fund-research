#!/usr/bin/env python3
"""Sanity check publish.py's HTML output before users hit it.

Without this, the dashboard at docs.sinostor.com.cn/hedge-fund-research.html
can silently render with missing fund sections, empty titles, or duplicate
style attributes — discoverable only by eyeballing the page.

What we check:
  1. every source in sources.json has exactly one fund section, and no other
     source has one (no off-by-one allowance: a vanished fund is an error)
  2. each section's article count equals an independent recount of the data
     (0 is right for a source with no articles yet, not an alarm)
  3. the header numbers (total / added this week / published this week /
     funds) equal the recount
  4. the article cards on the page (DOM + older-articles island) number the
     recount's total
  5. the JSON data islands are present and parse
  6. every inline script passes `node --check`
  7. no duplicate style="" attributes, no empty <h2></h2>
  8. with --written-after: the file was written by this run
  9. when the .gz sits beside the page: it decompresses to the same bytes
Checks 2-4 recount from data/articles.jsonl with code of their own; the only
thing shared with publish.py is publish.shown_articles, the definition of
which rows the page hides. Stage-4 audit (2026-10-09): the previous checks
read section headings and the count badge that publish.py writes, so a page
with every card removed, broken islands and a script syntax error passed.

publish.py runs checks 1-7 on the page before it goes live (_precheck);
run_pipeline.sh runs this script on the live file afterwards.

Exit codes:
  0 = all checks pass
  1 = at least one check failed
  2 = HTML file unreadable

A missing config/sources.json used to disarm every check and still exit 0 --
and publish.py degrades that same input in the opposite direction, rendering
zero fund sections under a headline reading "0 funds tracked". The one input
whose loss most damages the page turned the gate off. It is now an error
unless --allow-missing-sources is passed explicitly.

--written-after <epoch> asserts the HTML was written by the run that is
checking it. Stage 4 runs unconditionally after stages 1-3 fail, so a publish
that silently no-ops leaves the previous day's page on disk; without this the
checker blessed a file six years old.

Usage:
  python3 scripts/check_dashboard_html.py
  python3 scripts/check_dashboard_html.py --html-path <path>
  python3 scripts/check_dashboard_html.py --html-path <path> --json
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_HTML_PATH = Path("/var/www/overview/hedge-fund-research.html")
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
BJT = timezone(timedelta(hours=8))
WEEK_DAYS = 7
REQUIRED_ISLANDS = ("article-details-data", "taxonomy-data")
OPTIONAL_ISLANDS = ("older-articles-data",)   # absent when nothing is older than RECENT_DAYS
HEADER_STATS = ("total", "added-week", "published-week", "funds")


# ── individual checks ────────────────────────────────────────────────────────

def _extract_fund_section_ids(html: str) -> list[str]:
    """Return data-source-id values for every <section class="...fund-section...">."""
    pattern = re.compile(
        r'<section[^>]*class="[^"]*\bfund-section\b[^"]*"[^>]*data-source-id="([^"]+)"',
        re.IGNORECASE,
    )
    return pattern.findall(html)


def _extract_cluster_counts(html: str) -> list[tuple[str, int]]:
    """For each fund-section, return (sid, article_count_from_h2)."""
    out = []
    section_re = re.compile(
        r'<section[^>]*class="[^"]*\bfund-section\b[^"]*"[^>]*data-source-id="([^"]+)"[^>]*>'
        r'(.*?)</section>',
        re.IGNORECASE | re.DOTALL,
    )
    count_re = re.compile(r'<span\s+class="cluster-count"[^>]*>(\d+)', re.IGNORECASE)
    for sid, body in section_re.findall(html):
        m = count_re.search(body)
        out.append((sid, int(m.group(1)) if m else 0))
    return out


def _find_duplicate_style_tags(html: str) -> list[str]:
    """Return up to 5 example tags that have two style="..." attributes."""
    pattern = re.compile(r'(<\w+[^>]*\bstyle="[^"]*"[^>]*\bstyle="[^"]*"[^>]*>)',
                         re.IGNORECASE)
    return pattern.findall(html)[:5]


def _find_empty_h2(html: str) -> int:
    """Count <h2> with no visible text (allowing only whitespace + tags)."""
    return len(re.findall(r'<h2[^>]*>\s*</h2>', html, re.IGNORECASE))


def _strip_scripts(html: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S | re.I)


def _islands(html: str) -> dict[str, str]:
    return dict(re.findall(r'<script type="application/json" id="([\w-]+)">(.*?)</script>', html, re.S))


def header_stats(html: str) -> dict[str, int]:
    """The header numbers publish.py marks with data-stat."""
    return {k: int(v) for k, v in re.findall(r'data-stat="([\w-]+)">(\d+)<', html) if k in HEADER_STATS}


def built_date(html: str) -> date | None:
    """The BJT date the page was built for: the recount's "today", so a check
    run after midnight still counts the week the page counted."""
    m = re.search(r'data-built="(\d{4}-\d{2}-\d{2})"', html)
    return date.fromisoformat(m.group(1)) if m else None


def load_articles(path: Path = None) -> list[dict] | None:
    path = path or DATA_FILE
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue      # publish.load_articles skips them too
    return rows


def recount(articles: list[dict], source_ids: set[str], today: date | None) -> dict:
    """What the page should say, counted here rather than taken from publish.py."""
    sys.path.insert(0, str(BASE_DIR))
    import publish   # only for shown_articles: which rows the page hides
    today = today or datetime.now(BJT).date()
    first = today - timedelta(days=WEEK_DAYS - 1)
    shown = publish.shown_articles(articles, source_ids)
    per_source: dict[str, int] = {}
    added = published = 0
    for a in shown:
        per_source[a.get("source_id", "")] = per_source.get(a.get("source_id", ""), 0) + 1
        fetched = None
        if a.get("fetched_at"):
            try:
                stamp = datetime.fromisoformat(a["fetched_at"])
                fetched = (stamp if stamp.tzinfo else stamp.replace(tzinfo=BJT)).astimezone(BJT).date()
            except ValueError:
                fetched = None
        if fetched and first <= fetched <= today:
            added += 1
        stored = (a.get("date") or "").strip()
        try:
            day = date.fromisoformat(stored[:10]) if stored else None
        except ValueError:
            day = None
        if day is None or (day > today and fetched is None):
            continue      # undatable: neither shown as new nor counted
        effective = min(d for d in (day, today, fetched) if d is not None)
        if first <= effective <= today:
            published += 1
    return {"total": len(shown), "per_source": per_source, "added_week": added,
            "published_week": published, "funds": len(source_ids)}


def _node_check(scripts: list[str]) -> list[str]:
    """Indices + first error line of inline scripts node refuses to parse."""
    node = shutil.which("node")
    if node is None:
        return ["node is not installed, so the page's scripts were not checked"]
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, body in enumerate(scripts):
            f = Path(tmp) / f"s{i}.js"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                err = [ln for ln in r.stderr.splitlines() if "Error" in ln]
                bad.append(f"script {i}: {(err or ['parse error'])[0][:120]}")
    return bad


# ── orchestration ───────────────────────────────────────────────────────────

def load_expected_source_ids() -> set[str]:
    if not SOURCES_FILE.exists():
        return set()
    data = json.loads(SOURCES_FILE.read_text())
    return {s["id"] for s in data.get("sources", []) if s.get("id")}


def check_dashboard(html: str, expected_ids: set[str], *,
                    counts: dict | None = None,
                    allow_missing_sources: bool = False,
                    html_mtime: float | None = None,
                    written_after: float | None = None,
                    gz_path: Path | None = None,
                    check_scripts: bool = True) -> dict:
    """Run all checks; return {ok: bool, checks: [...]}.

    counts is recount()'s result; without it the data checks fail rather than
    pass, unless allow_missing_sources (development only)."""
    fund_ids = _extract_fund_section_ids(html)
    fund_id_set = set(fund_ids)
    sections = _extract_cluster_counts(html)
    dup_styles = _find_duplicate_style_tags(html)
    empty_h2 = _find_empty_h2(html)

    checks = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # 1. every expected source has a section. publish.py renders one for each
    # source, articles or not, so no allowance: a missing fund is a bug.
    if not expected_ids:
        add("fund_section_count", allow_missing_sources,
            "no expected sources — skipping (--allow-missing-sources)" if allow_missing_sources else
            "config/sources.json is missing or empty; every check below is comparing against nothing")
    else:
        missing = sorted(expected_ids - fund_id_set)
        add("fund_section_count", not missing,
            f"missing fund sections: {missing}" if missing else
            f"{len(fund_id_set)} sections / {len(expected_ids)} expected")

    # 2. data-source-id set is a subset of expected
    unknown = fund_id_set - expected_ids if expected_ids else set()
    add("fund_id_membership", not unknown,
        f"HTML shows unknown source ids: {sorted(unknown)} (not in sources.json)" if unknown else
        "all rendered ids are valid sources")

    # 3. duplicate sections (same data-source-id appears twice)
    dup_sections = sorted(sid for sid in fund_id_set if fund_ids.count(sid) > 1)
    add("no_duplicate_sections", not dup_sections,
        f"sections rendered more than once: {dup_sections}" if dup_sections else
        "every fund rendered at most once")

    # 4-6. the numbers on the page against the recount
    if counts is None:
        for name in ("section_article_counts", "header_stats", "article_cards"):
            add(name, allow_missing_sources, "no article data to recount against" +
                (" — skipping (--allow-missing-sources)" if allow_missing_sources else ""))
    else:
        wrong = [f"{sid}: page {n}, data {counts['per_source'].get(sid, 0)}"
                 for sid, n in sections if n != counts["per_source"].get(sid, 0)]
        add("section_article_counts", bool(sections) and not wrong,
            f"{len(wrong)} wrong: {wrong[:5]}" if wrong else
            ("no fund sections rendered at all" if not sections else
             f"all {len(sections)} sections match the data (zero-article sections included)"))

        want = {"total": counts["total"], "added-week": counts["added_week"],
                "published-week": counts["published_week"], "funds": counts["funds"]}
        got = header_stats(html)
        bad = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
        add("header_stats", not bad,
            f"page vs data: {bad}" if bad else f"header matches the data: {want}")

        islands = _islands(html)
        dom = len(re.findall(r'<article id="a-', _strip_scripts(html)))   # cards, not the CSS comment that names <article>
        try:
            older = len(json.loads(islands["older-articles-data"])) if "older-articles-data" in islands else 0
        except json.JSONDecodeError:
            older = 0     # reported by data_islands below
        add("article_cards", dom + older == counts["total"],
            f"{dom} cards + {older} older = {dom + older}, data has {counts['total']}")

    # 7. the data islands the page's scripts read
    islands = _islands(html)
    island_problems = [f"{name} missing" for name in REQUIRED_ISLANDS if name not in islands]
    for name in (*REQUIRED_ISLANDS, *OPTIONAL_ISLANDS):
        if name in islands:
            try:
                json.loads(islands[name])
            except json.JSONDecodeError as e:
                island_problems.append(f"{name} does not parse: {e.msg} at {e.pos}")
    add("data_islands", not island_problems,
        "; ".join(island_problems) if island_problems else
        f"{sum(n in islands for n in (*REQUIRED_ISLANDS, *OPTIONAL_ISLANDS))} islands parse")

    # 8. every inline script parses
    if check_scripts:
        scripts = [body for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I)
                   if "application/json" not in attrs and "src=" not in attrs and body.strip()]
        bad_scripts = _node_check(scripts)
        add("scripts_parse", not bad_scripts and bool(scripts),
            "; ".join(bad_scripts) if bad_scripts else
            (f"{len(scripts)} inline scripts parse" if scripts else "no inline scripts found"))

    # freshness — only claimed when a reference time was supplied, so the
    # report never implies it verified something it did not.
    if written_after is not None:
        fresh = html_mtime is not None and html_mtime >= written_after
        add("freshness", fresh,
            f"written {html_mtime:.0f} >= run start {written_after:.0f}" if fresh else
            f"stale: page mtime {html_mtime} predates this run's start "
            f"{written_after:.0f} — publish did not write it")

    # readers are served the .gz: it must be this page
    if gz_path is not None:
        try:
            same = gzip.open(gz_path, "rt", encoding="utf-8").read() == html
            add("gzip_matches_html", same, "the .gz decompresses to this page" if same else
                f"{gz_path} holds a different page than the .html beside it")
        except (OSError, EOFError) as e:
            add("gzip_matches_html", False, f"cannot read {gz_path}: {e}")

    add("no_duplicate_style_attrs", not dup_styles,
        f"found {len(dup_styles)} tags with duplicate style attrs (browsers silently drop the 2nd) "
        f"— example: {dup_styles[0][:120]}" if dup_styles else "no duplicate style attrs")
    add("no_empty_h2", not empty_h2,
        f"{empty_h2} empty <h2></h2> tag(s) (likely missing fund or cluster name)" if empty_h2 else
        "no empty h2 headers")

    return {"ok": all(c["passed"] for c in checks), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-path", default=str(DEFAULT_HTML_PATH))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--written-after", type=float, default=None,
                        help="epoch seconds; fail if the HTML predates it "
                             "(pass the pipeline's start time)")
    parser.add_argument("--allow-missing-sources", action="store_true",
                        help="treat an absent config/sources.json as a skip "
                             "rather than an error (development only)")
    args = parser.parse_args()

    html_path = Path(args.html_path)
    if not html_path.exists():
        print(f"ERROR: HTML file not found: {html_path}", file=sys.stderr)
        return 2

    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {html_path}: {exc}", file=sys.stderr)
        return 2

    expected_ids = load_expected_source_ids()
    try:
        html_mtime = html_path.stat().st_mtime
    except OSError:
        html_mtime = None
    articles = load_articles()
    counts = (recount(articles, expected_ids, built_date(html))
              if articles is not None and expected_ids else None)
    gz_path = html_path.with_name(html_path.name + ".gz")
    result = check_dashboard(html, expected_ids, counts=counts,
                             allow_missing_sources=args.allow_missing_sources,
                             html_mtime=html_mtime,
                             written_after=args.written_after,
                             gz_path=gz_path if gz_path.exists() else None)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for c in result["checks"]:
            mark = "✓" if c["passed"] else "✗"
            print(f"  {mark} {c['check']:30s} {c['detail']}")
        if result["ok"]:
            print(f"OK: dashboard html at {html_path} passes all sanity checks")
        else:
            failed = [c["check"] for c in result["checks"] if not c["passed"]]
            print(f"FAIL: {html_path} — failed checks: {', '.join(failed)}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
