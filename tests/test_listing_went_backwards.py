"""A listing that stops showing what it used to show (audit B3).

2026-09-06, acadian-asset: its insight listing defaulted to "Sort By:
Relevance" and returned ten pieces from 2016-2023. The log has the batch:

    ['2022-09-30', '2020-12-31', '2023-10-31', '2018-04-30', '2017-06-30',
     '2016-04-30', '2021-01-31', '2026-05-31', '2023-10-31', '2023-01-31']

Nine were ingested, summarised and published, while August's real posts fell
off the page. The guard that existed -- `date_sorted` plus a monotonicity
check -- had three holes: only 1 of 42 sources declares date_sorted; a page of
pure archive pieces that happens to be internally descending passes it; and
undated rows are excluded, so one dated row makes it vacuous. The log shows it
refusing one run that night and letting the next one through.

What actually happened is visible without any of that: the newest item the
listing showed (2026-05-31) was 92 days older than the newest article we had
already stored from that source (2026-08-31). Measured across all 42 sources
on 2026-09-21, that never happens normally -- 41 listings' newest item matched
our stored newest exactly, one was 3 days NEWER (an article we had not
ingested yet), and one returned nothing. A listing head that moves backwards
means the listing stopped showing what it showed yesterday.

BACKWARDS_GRACE_DAYS is 45: comfortably past any normal movement (zero), and
comfortably under the 92 days the real incident produced. A publisher
withdrawing its newest piece also moves the head back, which is worth refusing
a batch over too -- the next run recovers on its own once the listing settles.
"""
import json

import pytest

import fetch_articles as fa

SRC = {"id": "acadian-asset", "name": "A", "short_name": "A", "method": "playwright",
       "url": "https://www.acadian-asset.com/insights", "expected_hostname": ""}

INCIDENT = ['2022-09-30', '2020-12-31', '2023-10-31', '2018-04-30', '2017-06-30',
            '2016-04-30', '2021-01-31', '2026-05-31', '2023-10-31', '2023-01-31']


# One URL per date, so a stored row and the same row on the listing match --
# which is what a real listing does when it repeats yesterday's articles.
def _url(d):
    return f"https://www.acadian-asset.com/i/{d}"


def _stored(*dates):
    return [{"id": fa.article_id("acadian-asset", _url(d)), "source_id": "acadian-asset",
             "url": _url(d), "title": f"Stored {d}", "date": d} for d in dates]


def _listing(dates):
    return [{"title": f"Piece {d}", "url": _url(d), "date": d} for d in dates]


def _run(monkeypatch, listing, stored):
    monkeypatch.setitem(fa.FETCHERS, "acadian-asset", lambda s: _listing(listing))
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    return fa.fetch_source(dict(SRC), {r["id"] for r in stored}, dry_run=True,
                           existing_keys=fa.title_date_keys(stored), existing_rows=stored)


def test_the_acadian_incident_is_refused(monkeypatch):
    got = _run(monkeypatch, INCIDENT, _stored("2026-08-31", "2026-07-31"))
    assert got == [], "the batch that put 2016-2023 pieces on the page was accepted"


def test_an_ordinary_listing_is_accepted(monkeypatch):
    got = _run(monkeypatch, ["2026-09-18", "2026-08-31", "2026-07-31"],
               _stored("2026-08-31", "2026-07-31"))
    assert len(got) == 1 and got[0]["date"] == "2026-09-18"


def test_a_quiet_source_repeating_yesterdays_listing_is_accepted(monkeypatch):
    """The normal case measured on 41 of 42 sources: head unchanged."""
    got = _run(monkeypatch, ["2026-08-31", "2026-07-31"], _stored("2026-08-31", "2026-07-31"))
    assert got == []                      # nothing new, and no refusal either


def test_a_slow_sources_long_tail_is_not_a_refusal(monkeypatch):
    """principal-am publishes rarely: its ten most recent reach back to 2025,
    and on 2026-09-16 seven of them were ingested at once. The head is current,
    so nothing is wrong."""
    listing = ["2026-09-18", "2026-07-08", "2026-05-14", "2025-06-05", "2025-05-15"]
    got = _run(monkeypatch, listing, _stored("2026-09-18"))
    assert len(got) == 4


def test_a_source_with_no_history_is_accepted(monkeypatch):
    """A newly wired source legitimately backfills old pieces."""
    got = _run(monkeypatch, ["2019-01-31", "2018-06-30", "2017-03-31"], [])
    assert len(got) == 3


def test_a_listing_with_too_few_dates_is_not_judged(monkeypatch):
    got = _run(monkeypatch, ["2016-04-30"], _stored("2026-08-31"))
    assert len(got) == 1, "one old row is not evidence that the listing changed"


def test_an_undated_listing_is_not_judged(monkeypatch):
    listing = [{"title": f"P{i}", "url": f"https://www.acadian-asset.com/u/{i}", "date": None}
               for i in range(4)]
    monkeypatch.setitem(fa.FETCHERS, "acadian-asset", lambda s: listing)
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    stored = _stored("2026-08-31")
    got = fa.fetch_source(dict(SRC), {r["id"] for r in stored}, dry_run=True,
                          existing_keys=fa.title_date_keys(stored), existing_rows=stored)
    assert len(got) == 4


@pytest.mark.parametrize("head,refused", [("2026-07-25", False),   # 37 days back: inside the grace
                                          ("2026-06-20", True)])   # 72 days back
def test_the_grace_window(monkeypatch, head, refused):
    got = _run(monkeypatch, [head, "2026-01-31", "2025-12-31"], _stored("2026-08-31"))
    assert (got == []) is refused


def test_the_refusal_is_recorded_for_the_health_email(monkeypatch):
    """A refusal used to look exactly like a site being down: metrics (0,0,0,0)
    and nothing saying which it was."""
    seen = {}
    monkeypatch.setitem(fa.FETCHERS, "acadian-asset", lambda s: _listing(INCIDENT))
    monkeypatch.setattr(fa, "record_quality_metrics",
                        lambda *a, **k: seen.update(k) or seen.update({"args": a}))
    stored = _stored("2026-08-31")
    fa.fetch_source(dict(SRC), {r["id"] for r in stored}, dry_run=False,
                    existing_keys=fa.title_date_keys(stored), existing_rows=stored)
    assert seen.get("refusal") == "listing_head_moved_back", seen
