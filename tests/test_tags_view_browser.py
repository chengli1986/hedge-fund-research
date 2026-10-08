"""The Tags view's filtering runs in the page's JavaScript, so it is tested in a browser.

Every count on the rail and every list the reader sees is computed by
tvRender() at run time; asserting on the generated HTML would only prove the
data is there. Skipped where Chromium is not installed.
"""
from datetime import datetime, timedelta, timezone

import pytest

import publish

sync_api = pytest.importorskip("playwright.sync_api")
BJT = timezone(timedelta(hours=8))


def _day(n):
    return (datetime.now(BJT) - timedelta(days=n)).strftime("%Y-%m-%d")


def _art(i, tags, days=3, **kw):
    a = {"id": f"t{i}", "source_id": "man-group", "source_name": "Man", "title": f"Title {i}",
         "url": f"https://man.com/{i}", "date": _day(days), "summarized": True,
         "summary_en": f"summary {i}", "summary_zh": f"摘要 {i}", "key_takeaway_en": f"takeaway {i}",
         "key_takeaway_zh": f"要点 {i}", "tags": tags}
    a.update(kw)
    return a


ARTICLES = [
    _art(1, ["research", "us", "equities", "ai_tech"]),
    _art(2, ["research", "us", "govt_bonds"], summary_en="the strait of hormuz closed"),
    _art(3, ["event", "global", "commodities", "geopolitics_trade"]),
    _art(4, ["research", "europe", "equities"], days=200),          # older than RECENT_DAYS
]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    path = tmp_path_factory.mktemp("page") / "p.html"
    path.write_text(publish.generate_html(ARTICLES), encoding="utf-8")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:                      # no browser binary on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(path.as_uri())
        pg.errors = errors
        yield pg
        browser.close()


def _count(pg):
    return int(pg.inner_text("#tv-count b"))


def _listed(pg):
    return pg.eval_on_selector_all("#tv-list article.pool-article", "e => e.map(x => x.id)")


def _n(pg, tag):
    return int(pg.inner_text(f'#tv-rail [data-tvtag="{tag}"] .n'))


def test_tags_intersect_and_counts_say_what_would_remain(page):
    page.reload()
    assert _count(page) == 3 and _n(page, "us") == 2 and _n(page, "equities") == 1
    page.click('#tv-rail [data-tvtag="us"]')
    assert _count(page) == 2 and _n(page, "equities") == 1 and _n(page, "commodities") == 0
    page.click('#tv-rail [data-tvtag="equities"]')
    assert _listed(page) == ["a-t1"]
    page.click("#tv-clear")
    assert _count(page) == 3
    assert page.errors == []


def test_search_reaches_summaries_and_tag_names(page):
    page.reload()
    page.fill("#tv-q", "hormuz")
    assert _listed(page) == ["a-t2"]
    page.fill("#tv-q", "地缘政治")
    assert _listed(page) == ["a-t3"]


def test_older_articles_count_only_when_shown(page):
    page.reload()
    assert _n(page, "europe") == 0
    page.click("#btn-show-older")
    assert _count(page) == 4 and _n(page, "europe") == 1
    page.click("#btn-show-older")              # hidden again: loaded, but must not count
    assert _count(page) == 3 and _n(page, "europe") == 0


def test_a_chip_in_another_view_opens_the_tags_view_on_that_tag(page):
    page.reload()
    page.click('.view-btn[data-view="timeline"]')
    page.click('.timeline-wrap #a-t3 .tv-chip[data-tag="geopolitics_trade"]')
    assert page.get_attribute(".view-btn.active", "data-view") == "tags"
    assert _listed(page) == ["a-t3"]
