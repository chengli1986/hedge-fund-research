"""The page is checked before it goes live, against an independent recount.

Stage-4 audit (2026-10-09), reproduced on a real render:
  P6  a page with every article card removed, all three JSON islands broken and
      a JavaScript syntax error passed stage 5 (it looked at section headings
      and the cluster-count badge, which publish.py writes, never counts) --
      and stage 5 only ran after the page was already live;
  P5  a page missing a whole fund section passed (an off-by-one allowance),
      while a new source with no articles yet failed (zero-count rule);
  16  nothing compared the header numbers with the data at all;
  17  "new this week" was an 8-day window and counted only by publish date.
Every test here failed on the code before the fix.
"""
import gzip
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import publish  # noqa: E402

_spec = importlib.util.spec_from_file_location("cdh_pre", REPO / "scripts" / "check_dashboard_html.py")
cdh = importlib.util.module_from_spec(_spec)
sys.modules["cdh_pre"] = cdh
_spec.loader.exec_module(cdh)

NOW = datetime.now(publish.BJT)


def _day(n):
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d")


def _art(i, sid, published_days_ago, fetched_days_ago, **extra):
    return {"id": f"a{i:03d}", "source_id": sid, "title": f"Title {i}", "url": f"https://example.com/{i}",
            "date": _day(published_days_ago), "date_raw": _day(published_days_ago),
            "fetched_at": (NOW - timedelta(days=fetched_days_ago)).isoformat(), "summarized": False, **extra}


# gmo: two this week (one published earlier but only collected now);
# kkr: one old (goes to the older-articles island); man-group: day 6 is in the
# 7-day window, day 7 is not; one duplicate_body row that must stay hidden.
ARTICLES = [
    _art(1, "gmo", 1, 1),
    _art(2, "gmo", 40, 2),
    _art(3, "kkr", 200, 200),
    _art(4, "man-group", 6, 6),
    _art(5, "man-group", 7, 7),
    _art(6, "man-group", 2, 2, analysis_label="duplicate_body"),
]


@pytest.fixture(scope="module")
def page():
    return publish.generate_html(ARTICLES)


def _ids():
    return cdh.load_expected_source_ids()


def _check(html, articles=ARTICLES, **kw):
    return cdh.check_dashboard(html, _ids(), counts=cdh.recount(articles, _ids(), cdh.built_date(html)), **kw)


def _failed(result):
    return {c["check"] for c in result["checks"] if not c["passed"]}


# ── a real render passes, including sources with no articles ──────────────────

def test_a_real_render_passes(page):
    result = _check(page)
    assert result["ok"], [c for c in result["checks"] if not c["passed"]]


def test_a_source_with_no_articles_is_not_an_alarm(page):
    counts = cdh.recount(ARTICLES, _ids(), cdh.built_date(page))
    assert counts["per_source"].get("aqr", 0) == 0
    assert "section_article_counts" not in _failed(_check(page))


# ── the independent recount ────────────────────────────────────────────────────

def test_recount_numbers(page):
    counts = cdh.recount(ARTICLES, _ids(), cdh.built_date(page))
    assert counts["total"] == 5                      # the duplicate_body row is hidden
    assert counts["per_source"]["gmo"] == 2 and counts["per_source"]["man-group"] == 2
    assert counts["added_week"] == 3                 # a001, a002, a004 (day 7 is out)
    assert counts["published_week"] == 2             # a001, a004 (a002 was published 40 days ago)
    assert counts["funds"] == len(_ids())


def test_header_shows_both_weekly_numbers(page):
    stats = cdh.header_stats(page)
    assert stats == {"total": 5, "added-week": 3, "published-week": 2, "funds": len(_ids())}
    assert "本周新收录 <b>3</b>" in page and "其中本周发表 <b>2</b>" in page


# ── the damage the old check let through ──────────────────────────────────────

def _drop_section(html, sid):
    return re.sub(rf'<section class="cluster fund-section" data-source-id="{sid}".*?</section>', "", html,
                  count=1, flags=re.S)


def test_a_missing_fund_section_fails(page):
    assert "fund_section_count" in _failed(_check(_drop_section(page, "aqr")))


def test_a_wrong_section_count_fails(page):
    broken = page.replace('<span class="cluster-count">2 articles', '<span class="cluster-count">9 articles', 1)
    assert broken != page
    assert "section_article_counts" in _failed(_check(broken))


def test_removed_article_cards_fail(page):
    head, sep, rest = page.partition('<article id="a-a001"')
    broken = head + rest[rest.index("</article>") + len("</article>"):]
    assert "article_cards" in _failed(_check(broken))


@pytest.mark.parametrize("island", ["older-articles-data", "article-details-data", "taxonomy-data"])
def test_a_broken_data_island_fails(page, island):
    broken = page.replace(f'id="{island}">', f'id="{island}">{{broken', 1)
    assert broken != page
    assert "data_islands" in _failed(_check(broken))


def test_a_javascript_syntax_error_fails(page):
    marker = "function toggleLang"
    assert marker in page
    broken = page.replace(marker, "function ( { " + marker, 1)
    assert "scripts_parse" in _failed(_check(broken))


def test_a_wrong_header_number_fails(page):
    broken = page.replace('data-stat="total">5<', 'data-stat="total">6<', 1)
    assert broken != page
    assert "header_stats" in _failed(_check(broken))


def test_a_gz_that_disagrees_with_the_html_fails(tmp_path, page):
    html = tmp_path / "p.html"
    html.write_text(page, encoding="utf-8")
    with gzip.open(tmp_path / "p.html.gz", "wt", encoding="utf-8") as f:
        f.write(page.replace("Title 1", "Title one"))
    result = _check(page, gz_path=tmp_path / "p.html.gz")
    assert "gzip_matches_html" in _failed(result)


# ── checked BEFORE it goes live ───────────────────────────────────────────────

def test_publish_keeps_yesterdays_page_when_the_check_fails(tmp_path, monkeypatch):
    out = tmp_path / "page.html"
    out.write_text("yesterday", encoding="utf-8")
    monkeypatch.setattr(publish, "load_articles", lambda: list(ARTICLES))
    real = publish.generate_html
    monkeypatch.setattr(publish, "generate_html", lambda arts: _drop_section(real(arts), "aqr"))
    synced = []
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: synced.append(1) or True)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(out)])
    assert publish.main() == publish.PRECHECK_FAILED
    assert out.read_text(encoding="utf-8") == "yesterday"
    assert not (tmp_path / "page.html.gz").exists() and not synced


def test_publish_goes_live_when_the_check_passes(tmp_path, monkeypatch):
    out = tmp_path / "page.html"
    monkeypatch.setattr(publish, "load_articles", lambda: list(ARTICLES))
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: True)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(out)])
    assert publish.main() == 0
    assert 'data-stat="total">5<' in out.read_text(encoding="utf-8")


def test_run_pipeline_reads_the_precheck_exit_and_skips_stage_5():
    script = (REPO / "run_pipeline.sh").read_text()
    assert re.search(r'publish_rc -eq 4 \]\]; then\s*\n\s*failed_stages\+=\("Stage4:precheck"\)', script), (
        "exit 4 from publish.py is not recorded as Stage4:precheck")
    stage5 = [ln for ln in script.splitlines() if "Stage4:publish" in ln and "failed_stages[*]" in ln]
    assert stage5 and all("Stage4:precheck" in ln for ln in stage5), stage5


# ── pre-merge review, 2026-10-10 ─────────────────────────────────────────────

def test_the_checker_reads_the_data_file_line_by_line_like_publish(tmp_path):
    """U+2028 inside a title: splitlines() cut that row in two and stage 5
    counted one article fewer than the page, every night."""
    rows = [_art(1, "gmo", 1, 1, title="Line separator"), _art(2, "gmo", 2, 2)]
    path = tmp_path / "a.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert len(cdh.load_articles(path)) == len(rows)


def test_a_utc_timestamp_does_not_make_the_page_and_the_check_disagree():
    """fetched_at in UTC, the evening before (BJT) the first day of the week."""
    first = (NOW - timedelta(days=publish.WEEK_DAYS - 1)).date()
    utc_evening = datetime.combine(first, datetime.min.time()) - timedelta(hours=4)
    row = _art(9, "gmo", publish.WEEK_DAYS - 1, 0)
    row["fetched_at"] = utc_evening.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    articles = ARTICLES + [row]
    html = publish.generate_html(articles)
    assert publish._precheck(html, articles) == []


def test_emptied_older_cards_fail(page):
    m = re.search(r'id="older-articles-data">(.*?)</script>', page, flags=re.S)
    emptied = json.dumps([""] * len(json.loads(m.group(1).replace("<\\/", "</"))))
    broken = page.replace(m.group(1), emptied, 1)
    assert "article_cards" in _failed(_check(broken))


def test_the_data_date_in_the_header_is_escaped():
    row = _art(9, "gmo", 1, 0)            # yesterday's date, so it is the newest: "Data through"
    row["date"] = row["date"] + '<img src=x onerror="alert(1)">'
    html = publish.generate_html(ARTICLES + [row])
    header = html[html.index('<div class="stats"'):html.index('</div>', html.index('<div class="stats"'))]
    # The stored date is now read as YYYY-MM-DD first (as the stage-5 recount
    # reads it), so the markup never reaches the header; it is escaped as well.
    assert '<img src=x onerror' not in header and f"Data through {_day(1)}" in header


@pytest.mark.parametrize("bad", ["n/a", "October 2026", 20261001, "2026-10-10T23:00:00"])
def test_a_date_that_is_not_plain_iso_counts_the_same_on_the_page_and_in_the_check(bad):
    """Compared as raw text these made the page and the recount disagree on
    "published this week" (the pre-check then froze the page); an int crashed."""
    row = _art(9, "gmo", 1, 1)
    row["date"] = bad if bad != "2026-10-10T23:00:00" else _day(0) + "T23:00:00"
    row.pop("fetched_at")
    articles = ARTICLES + [row]
    html = publish.generate_html(articles)
    assert publish._precheck(html, articles) == []
