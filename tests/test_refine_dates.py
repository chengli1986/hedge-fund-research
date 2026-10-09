"""Month-only listing dates get the real day from the article page (scripts/refine_dates.py)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import requests

import fetch_articles as fa
import publish

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "refine_dates.py"
spec = importlib.util.spec_from_file_location("refine_dates", SCRIPT)
rd = importlib.util.module_from_spec(spec)
sys.modules["refine_dates"] = rd
spec.loader.exec_module(rd)

TODAY = "2026-10-08"


def _row(i, **kw):
    r = {"id": f"r{i}", "source_id": "kkr", "title": f"T{i}", "url": f"https://www.kkr.com/insights/t{i}",
         "date": "2026-10-31", "date_raw": "October 2026", "fetched_at": "2026-10-08T03:50:00+08:00"}
    r.update(kw)
    return r


def _store(tmp_path, rows):
    p = tmp_path / "articles.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _read(p):
    return {r["id"]: r for r in (json.loads(l) for l in p.read_text().splitlines() if l)}


def _page(html=None, status=200):
    def get(url):
        if status != 200:
            resp = requests.Response()
            resp.status_code = status
            raise requests.HTTPError(f"{status}", response=resp)
        return html
    return get


def test_the_page_date_replaces_the_month_end_and_the_listing_label_is_kept(tmp_path):
    p = _store(tmp_path, [_row(1)])
    rd.run(p, get=_page('<script>{"datePublished":"2026-10-07T04:00:00Z"}</script>'), report=None, today=TODAY)
    r = _read(p)["r1"]
    assert (r["date"], r["date_basis"], r["date_listed"]) == ("2026-10-07", "page", "2026-10-31")
    assert r["date_raw"] == "October 2026"        # fetch_articles needs it to recognise the article


def test_an_escaped_json_date_is_read(tmp_path):
    html = r'"datePublished\":\"2026-10-01\",\"dateModified\":\"2026-09-27\"'
    p = _store(tmp_path, [_row(1)])
    rd.run(p, get=_page(html), report=None, today=TODAY)
    assert _read(p)["r1"]["date"] == "2026-10-01"


@pytest.mark.parametrize("html", [
    '<script>{"datePublished":"2026-09-12"}</script>',      # another month: a sidebar, not this article
    '<p>no date anywhere</p>',
])
def test_an_implausible_or_missing_date_falls_back_to_the_day_we_first_saw_it(tmp_path, html):
    p = _store(tmp_path, [_row(1)])
    rd.run(p, get=_page(html), report=None, today=TODAY)
    r = _read(p)["r1"]
    assert (r["date"], r["date_basis"]) == ("2026-10-08", "first_seen")


def test_first_seen_after_the_month_keeps_the_month_end(tmp_path):
    p = _store(tmp_path, [_row(1, date="2026-03-31", date_raw="March 2026", fetched_at="2026-04-20T03:00:00+08:00")])
    rd.run(p, get=_page("<p>none</p>"), report=None, today=TODAY)
    assert (_read(p)["r1"]["date"], _read(p)["r1"]["date_basis"]) == ("2026-03-31", "month_end")


def test_a_day_in_the_url_is_used_without_fetching(tmp_path):
    p = _store(tmp_path, [_row(1, source_id="man-group", url="https://www.man.com/insights/views-from-the-floor-2026-6-oct")])

    def get(url):
        raise AssertionError("fetched although the URL has the day")
    rd.run(p, get=get, report=None, today=TODAY)
    assert (_read(p)["r1"]["date"], _read(p)["r1"]["date_basis"]) == ("2026-10-06", "url")


def test_a_failed_fetch_is_retried_then_falls_back(tmp_path):
    p = _store(tmp_path, [_row(1)])
    for _ in range(rd.FETCH_TRIES - 1):
        rd.run(p, get=_page(status=503), report=None, today=TODAY)
        assert "date_basis" not in _read(p)["r1"] and _read(p)["r1"]["date"] == "2026-10-31"
    rd.run(p, get=_page(status=503), report=None, today=TODAY)
    assert _read(p)["r1"]["date_basis"] == "first_seen"


def test_a_removed_page_falls_back_at_once(tmp_path):
    p = _store(tmp_path, [_row(1)])
    rd.run(p, get=_page(status=404), report=None, today=TODAY)
    assert _read(p)["r1"]["date_basis"] == "first_seen"


def test_rows_with_a_real_day_or_already_refined_are_left_alone(tmp_path):
    rows = [_row(1, date="2026-10-05", date_raw="October 5, 2026"),
            _row(2, date="2026-10-02", date_basis="page", date_listed="2026-10-31")]
    p = _store(tmp_path, rows)
    before = p.read_text()
    rd.run(p, get=_page('<script>{"datePublished":"2026-10-03"}</script>'), report=None, today=TODAY)
    assert p.read_text() == before


def test_only_date_fields_change_and_other_writes_are_kept(tmp_path):
    p = _store(tmp_path, [_row(1, summary_en="old"), _row(2)])

    def get(url):
        rows = _read(p)
        rows["r2"]["summary_en"] = "written meanwhile by the nightly run"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows.values()))
        return '<script>{"datePublished":"2026-10-07"}</script>'
    rd.run(p, get=get, report=None, today=TODAY, workers=1)
    after = _read(p)
    assert after["r2"]["summary_en"] == "written meanwhile by the nightly run"
    assert {k for k in after["r1"] if k not in _row(1, summary_en="")} == {"date_listed", "date_basis"}


def test_the_listing_showing_the_month_again_is_not_a_new_issue():
    """fetch_articles sees "October 2026" (-> 10-31) again for a row now dated 10-07."""
    stored = [dict(_row(1), id=fa.article_id("kkr", "https://www.kkr.com/insights/t1"),
                   date="2026-10-07", date_basis="page", date_listed="2026-10-31")]
    src = {"id": "kkr", "name": "KKR", "short_name": "KKR", "method": "api", "url": "https://x/",
           "expected_hostname": ""}
    listing = [{"title": "T1", "url": "https://www.kkr.com/insights/t1", "date": "2026-10-31",
                "date_raw": "October 2026"}]
    mp = pytest.MonkeyPatch()
    mp.setitem(fa.FETCHERS, "kkr", lambda s: [dict(r) for r in listing])
    mp.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    try:
        got = fa.fetch_source(src, {stored[0]["id"]}, dry_run=True,
                              existing_keys=fa.title_date_keys(stored), existing_rows=stored)
    finally:
        mp.undo()
    assert got == []


def test_the_page_shows_the_exact_day_once_found_and_the_month_otherwise():
    assert publish._display_date(_row(1, date="2026-10-07", date_basis="page")) == "2026-10-07"
    assert publish._display_date(_row(1, date="2026-10-08", date_basis="first_seen")) == "October 2026"


def test_a_page_date_weeks_after_we_first_saw_the_article_is_not_believed(tmp_path):
    """acadian 'Quick Take: The New Challenge': first seen 06-12, datePublished 06-28."""
    p = _store(tmp_path, [_row(1, date="2026-06-30", date_raw="June 2026", fetched_at="2026-06-12T04:00:00+08:00")])
    rd.run(p, get=_page('<script>{"datePublished":"2026-06-28T00:00:00Z"}</script>'), report=None, today=TODAY)
    assert (_read(p)["r1"]["date"], _read(p)["r1"]["date_basis"]) == ("2026-06-12", "first_seen")


def _fetch(stored, listing, source=None):
    src = {"id": "baillie-gifford", "name": "BG", "short_name": "BG", "method": "api", "url": "https://x/",
           "expected_hostname": ""}
    src.update(source or {})
    mp = pytest.MonkeyPatch()
    mp.setitem(fa.FETCHERS, src["id"], lambda s: [dict(r) for r in listing])
    mp.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    try:
        return fa.fetch_source(src, {r["id"] for r in stored}, dry_run=True,
                               existing_keys=fa.title_date_keys(stored), existing_rows=stored)
    finally:
        mp.undo()


BG_URL = "https://www.bailliegifford.com/en/uk/insights/ic-article/2026-q3-em-enough-is-not-enough-10065229/"


def test_an_edited_title_on_a_refined_row_is_still_that_article():
    """2026-10-09: refine_dates moved the row to 09-07, the listing still said September
    (09-30) and the site had renamed it, so it was ingested again as a new issue."""
    stored = [{"id": fa.article_id("baillie-gifford", BG_URL), "source_id": "baillie-gifford",
               "title": "EM: enough is not enough", "url": BG_URL, "date": "2026-09-07",
               "date_raw": "September 2026", "date_listed": "2026-09-30", "date_basis": "page"}]
    listing = [{"title": "Emerging markets: enough is not enough", "url": BG_URL,
                "date": "2026-09-30", "date_raw": "September 2026"}]
    assert _fetch(stored, listing) == []


def test_a_renamed_slug_of_a_refined_row_is_still_that_article():
    """title_date_keys catches slug renames by (title, date); the date it held was
    the listing's, which refine_dates replaced."""
    stored = [{"id": fa.article_id("baillie-gifford", BG_URL), "source_id": "baillie-gifford",
               "title": "EM: enough is not enough", "url": BG_URL, "date": "2026-09-07",
               "date_raw": "September 2026", "date_listed": "2026-09-30", "date_basis": "page"}]
    listing = [{"title": "EM: enough is not enough", "url": BG_URL.replace("10065229", "10065230"),
                "date": "2026-09-30", "date_raw": "September 2026"}]
    assert _fetch(stored, listing) == []


def test_a_later_month_on_a_refined_row_is_still_a_new_issue():
    """The fix must not swallow a real next issue at a reused URL."""
    stored = [{"id": fa.article_id("baillie-gifford", BG_URL), "source_id": "baillie-gifford",
               "title": "Monthly letter", "url": BG_URL, "date": "2026-09-07",
               "date_raw": "September 2026", "date_listed": "2026-09-30", "date_basis": "page"}]
    listing = [{"title": "Monthly letter", "url": BG_URL, "date": "2026-10-31", "date_raw": "October 2026"}]
    assert [a["date"] for a in _fetch(stored, listing)] == ["2026-10-31"]
