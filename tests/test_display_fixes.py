"""What the reader sees (stage-4 audit, 2026-10-09; reproduced on a real render).

P4  founded / aum / hq went into the Sources cards unescaped: an
    `<img onerror>` in a profile ran on the page.
P7  the timeline's "Load more" said 1686 remaining where 851 would open, and
    never went away: its script found the Tags view's #tv-more button first.
    It was also English only.
    The Sources cards were 430px wide on a 390px phone: the mobile rule came
    before the desktop rule it was meant to override.
14  21 urls carry several issues (a weekly page the fund overwrites); every
    old row links to today's issue without saying so, and same-titled rows
    were told apart only by the small date column.
    Capital Group's 25 rows have no date and showed nothing; de-shaw's 10
    year-only rows showed "2026-01-01", a day nobody published on.
Every test here failed on the code before the fix.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import publish

BJT = timezone(timedelta(hours=8))


def _day(n):
    return (datetime.now(BJT) - timedelta(days=n)).strftime("%Y-%m-%d")


def _art(i, sid="man-group", days=3, **kw):
    a = {"id": f"d{i:03d}", "source_id": sid, "source_name": sid, "title": f"Title {i}",
         "url": f"https://example.com/{i}", "date": _day(days), "date_raw": _day(days),
         "summarized": False}
    a.update(kw)
    return a


# ── P4: profile fields are escaped ───────────────────────────────────────────

def test_profile_fields_are_escaped_in_the_sources_cards(monkeypatch):
    evil = '<img src=x onerror="alert(1)">'
    profile = dict(publish._FUND_PROFILES["man-group"], founded=evil, aum=evil, hq=evil)
    monkeypatch.setitem(publish._FUND_PROFILES, "man-group", profile)
    page = publish._build_sources_view({"man-group": {"name": "Man Group"}})
    assert "<img" not in page
    assert page.count("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;") >= 4   # founded x2, aum, hq


# ── 14: several issues behind one url ────────────────────────────────────────

WEEKLY = "https://example.com/weekly-update"
ISSUES = [_art(1, days=2, url=WEEKLY, title="Weekly Update"),
          _art(2, days=9, url=WEEKLY, title="Weekly Update"),
          _art(3, days=16, url=WEEKLY, title="Weekly Update"),
          _art(4, days=5, title="One-off note")]


def _rows(page):
    """Every article's markup in page order, older-articles island included."""
    found = re.findall(r'<article id="a-.*?</article>', page, flags=re.S)
    island = re.search(r'id="older-articles-data">(.*?)</script>', page, flags=re.S)
    if island:
        found += json.loads(island.group(1))
    return found


def _row(page, aid):
    return next(r for r in _rows(page) if r.startswith(f'<article id="a-{aid}"'))


def test_only_the_older_issues_say_the_link_now_shows_the_latest():
    page = publish.generate_html(ISSUES)
    assert "url-reused" not in _row(page, "d001")
    for aid in ("d002", "d003"):
        assert "url-reused" in _row(page, aid) and "原文网址已更新为最新一期" in _row(page, aid)
    assert "url-reused" not in _row(page, "d004")


def test_same_titled_issues_carry_their_date_in_the_headline():
    page = publish.generate_html(ISSUES)
    for aid, days in (("d001", 2), ("d002", 9), ("d003", 16)):
        assert f"Weekly Update · {_day(days)}</a>" in _row(page, aid)
    assert ">One-off note</a>" in _row(page, "d004")


# ── dates we do not have ─────────────────────────────────────────────────────

def test_an_undated_row_says_so():
    page = publish.generate_html([_art(1, sid="capital-group", date=None, date_raw="")])
    row = _row(page, "d001")
    assert "日期未知" in row and "Undated" in row


def test_a_year_only_row_shows_the_year_and_sorts_at_the_end_of_it():
    year = datetime.now(BJT).year
    rows = [_art(1, sid="de-shaw", date=f"{year}-01-01", date_raw=str(year)),
            _art(2, days=1)]
    assert publish._display_date(rows[0]) == str(year)
    page = publish.generate_html(rows)
    assert f'<span class="date">{year}</span>' in _row(page, "d001")
    order = [r[len('<article id="a-'):r.index('"', len('<article id="a-'))] for r in _rows(page)]
    assert order == ["d002", "d001"]


# ── in a browser: the timeline button and the phone layout ───────────────────

RECENT, OLDER = 25, 3
FEED = ([_art(i, days=1 + i % 40) for i in range(RECENT)]
        + [_art(100 + i, days=200 + i) for i in range(OLDER)])


@pytest.fixture(scope="module")
def browser(chromium):
    return chromium


@pytest.fixture(scope="module")
def feed_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("feed") / "p.html"
    path.write_text(publish.generate_html(FEED), encoding="utf-8")
    return path


def _open(browser, path, width=1280):
    pg = browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(path.as_uri())
    pg.errors = errors
    return pg


def test_load_more_counts_what_it_will_open_and_then_goes_away(browser, feed_path):
    pg = _open(browser, feed_path)
    pg.click('.view-btn[data-view="timeline"]')
    more = "#tl-more"
    assert pg.is_visible(more)
    assert "5" in pg.inner_text(more), pg.inner_text(more)       # 25 recent - 20 shown
    pg.click(more)
    assert pg.is_hidden(more)
    assert pg.eval_on_selector_all("#view-timeline article.pool-article", "e => e.filter("
                                   "x => x.offsetParent !== null).length") == RECENT
    assert pg.errors == []
    pg.close()


def test_the_rendered_button_already_counts_only_recent_rows():
    """Before any script runs (and in the stage-5 checker's view of the file)."""
    page = publish.generate_html(FEED)
    assert 'id="tl-more"' in page
    assert "Load more (5 remaining)" in page and "加载更多（还有 5 篇）" in page


def test_load_more_follows_the_older_toggle_and_the_language(browser, feed_path):
    pg = _open(browser, feed_path)
    pg.click('.view-btn[data-view="timeline"]')
    pg.click("#btn-show-older")
    assert pg.inner_text("#tl-more") == "Load more (8 remaining)"   # 28 - 20 once older rows show
    pg.click("#btn-show-older")
    assert pg.inner_text("#tl-more") == "Load more (5 remaining)"   # hidden again: not counted
    pg.click("text=CN / EN")
    assert pg.inner_text("#tl-more") == "加载更多（还有 5 篇）"
    pg.click("#btn-show-older")                                     # redrawn while in CN
    assert pg.inner_text("#tl-more") == "加载更多（还有 8 篇）"
    assert pg.errors == []
    pg.close()


def test_sources_cards_fit_a_phone(browser, feed_path):
    pg = _open(browser, feed_path, width=390)
    pg.click('.view-btn[data-view="sources"]')
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    widest = pg.eval_on_selector_all(".source-card", "e => Math.max(...e.map(x => x.getBoundingClientRect().right))")
    assert widest <= 390
    pg.close()


def test_an_undated_topic_page_is_called_a_topic_page():
    """Bridgewater's three undated rows are topic hubs ("Explore a selection of
    our insights"), listing 11-14 pieces with their own dates; the hub has none.
    sources.json says so for the source, and the row says "Topic page"."""
    assert publish._load_sources()["bridgewater"].get("undated_rows_are") == "topic_pages"
    page = publish.generate_html([_art(1, sid="bridgewater", date=None, date_raw=""),
                                  _art(2, sid="capital-group", date=None, date_raw=""),
                                  _art(3, sid="bridgewater", days=2)])
    hub, undated, dated = _row(page, "d001"), _row(page, "d002"), _row(page, "d003")
    assert "专题页" in hub and "Topic page" in hub and "日期未知" not in hub
    assert "日期未知" in undated and "专题页" not in undated
    assert "专题页" not in dated


def test_load_more_appears_when_older_rows_are_shown_on_a_quiet_feed(browser, tmp_path):
    """15 recent rows fit on one screen, so the button was not rendered at all;
    after "Show older", rows past the first 20 could never be opened."""
    path = tmp_path / "quiet.html"
    path.write_text(publish.generate_html([_art(i, days=1 + i) for i in range(15)]
                                          + [_art(200 + i, days=200 + i) for i in range(10)]),
                    encoding="utf-8")
    pg = _open(browser, path)
    pg.click('.view-btn[data-view="timeline"]')
    assert pg.is_hidden("#tl-more")
    pg.click("#btn-show-older")
    assert pg.inner_text("#tl-more") == "Load more (5 remaining)"
    pg.click("#tl-more")
    assert pg.eval_on_selector_all("#view-timeline article.pool-article",
                                   "e => e.filter(x => x.offsetParent !== null).length") == 25
    assert pg.errors == []
    pg.close()


def test_show_older_does_not_fold_an_expanded_timeline(browser, feed_path):
    """It rebuilt the timeline and dropped back to 20 rows (full re-review)."""
    pg = _open(browser, feed_path)
    pg.click('.view-btn[data-view="timeline"]')
    pg.click("#tl-more")
    pg.click("#btn-show-older")
    visible = pg.eval_on_selector_all("#view-timeline article.pool-article",
                                      "e => e.filter(x => x.offsetParent !== null).length")
    assert visible == RECENT + OLDER and pg.is_hidden("#tl-more")
    assert pg.errors == []
    pg.close()
