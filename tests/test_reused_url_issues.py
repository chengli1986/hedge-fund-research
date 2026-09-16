"""A new issue at a reused URL must not be dropped in silence (audit B2).

A series page keeps one URL and republishes: franklin's /articles/series/...,
gsam's Market Pulse, wellington's Monthly Market Review, mfs' weekly. 8cacc65
taught the fetcher to accept a later issue at a stored URL, but only when the
listing row differed from the stored one in BOTH title and date -- to keep an
edited headline or a corrected date from being ingested twice.

The cost of that "both" was found on 2026-09-16 and reproduced: a series whose
headline is stable ("Weekly Market Update", every week) matches on title, so
every issue after the first hits `continue` -- with no log line at all, while
the source still reports "10 articles found, 0 new" and reads healthy. All
four sources that reuse URLs happen to retitle each issue today, so this is a
trap rather than an active loss; it springs the day one of them stops.

The fix prefers ingesting to dropping, because the two errors are not equal:
  · a wrongly ingested duplicate is visible, and stage 3 already refuses to
    publish a body it has published before (analysis_label duplicate_body);
  · a wrongly dropped issue is invisible and permanent.
So a reused URL whose date moved FORWARD is a new issue even when the title is
unchanged. A date that moved backwards is still treated as a correction, and
every skip now says why in the log.
"""
import logging

import pytest

import fetch_articles as fa

URL = "https://www.example.com/insights/series/weekly-market-update"
SRC = {"id": "gsam", "name": "GSAM", "short_name": "GSAM", "method": "api",
       "url": "https://x/", "expected_hostname": ""}
# Shaped like a real gsam row: its listing carries a full timestamp, so the
# stored date is a real publication day, not a normalised month.
STORED = {"id": fa.article_id("gsam", URL), "source_id": "gsam", "url": URL,
          "title": "Weekly Market Update", "date": "2026-09-01",
          "date_raw": "2026-09-01T05:00:00.000Z"}


def _run(monkeypatch, listing, stored=None):
    stored = [dict(STORED)] if stored is None else stored
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: [dict(r) for r in listing])
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    keys = fa.title_date_keys(stored)
    return fa.fetch_source(dict(SRC), {r["id"] for r in stored}, dry_run=True,
                           existing_keys=keys, existing_rows=stored)


def test_a_stable_headline_series_is_ingested_on_its_next_date(monkeypatch):
    """The B2 case: same title, later date. Was 0 new, in silence."""
    got = _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": "2026-09-08"}])
    assert len(got) == 1
    assert got[0]["id"] == fa.article_id("gsam", URL, issue_date="2026-09-08")


def test_a_retitled_issue_is_still_ingested(monkeypatch):
    """The case 8cacc65 added; it must keep working."""
    got = _run(monkeypatch, [{"title": "Weekly Market Update — September 8", "url": URL,
                              "date": "2026-09-08"}])
    assert len(got) == 1


def test_the_same_issue_seen_again_is_not_ingested_twice(monkeypatch):
    issue = {"title": "Weekly Market Update", "url": URL, "date": "2026-09-08"}
    first = _run(monkeypatch, [issue])
    stored = [dict(STORED), first[0]]
    assert _run(monkeypatch, [issue], stored=stored) == []


def test_an_unchanged_listing_row_is_not_ingested(monkeypatch):
    assert _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": "2026-09-01"}]) == []


def test_a_date_moved_backwards_is_a_correction_not_an_issue(monkeypatch):
    """A publisher fixing a date must not mint a second row."""
    got = _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": "2026-08-25"}])
    assert got == []


@pytest.mark.parametrize("date", ["2026-09-02", "2026-09-03", "2026-09-05"])
def test_a_small_forward_shift_is_a_correction_not_an_issue(monkeypatch, date):
    """Measured on every reused URL in the store: the shortest real gap between
    consecutive issues is 7 days (mfs week-in-review), the rest 28-126. A shift
    of a day or two is a publisher fixing a date."""
    assert _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": date}]) == []


def test_the_shortest_real_cadence_is_still_recognised(monkeypatch):
    """mfs republishes week-in-review.html every 7 days."""
    got = _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": "2026-09-08"}])
    assert len(got) == 1


def test_an_unreadable_date_does_not_mint_an_issue(monkeypatch):
    got = _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": "not-a-date"}])
    assert got == []


def test_a_retitled_row_on_the_same_date_is_an_edit_not_an_issue(monkeypatch):
    got = _run(monkeypatch, [{"title": "Weekly Market Update (revised)", "url": URL,
                              "date": "2026-09-01"}])
    assert got == []


@pytest.mark.parametrize("listing,reason", [
    ([{"title": "Weekly Market Update", "url": URL, "date": "2026-08-25"}], "correction"),
    ([{"title": "Weekly Market Update (revised)", "url": URL, "date": "2026-09-01"}], "same date"),
    ([{"title": "Weekly Market Update", "url": URL, "date": None}], "no date"),
])
def test_every_skip_at_a_reused_url_says_why(monkeypatch, caplog, listing, reason):
    """The silence was the defect: a dropped issue left no trace anywhere."""
    with caplog.at_level(logging.INFO, logger=fa.log.name):
        _run(monkeypatch, listing)
    lines = [r.getMessage() for r in caplog.records]
    assert any(URL in m and reason in m for m in lines), lines


class TestMonthGranularDates:
    """research-affiliates dates its pieces "FEB 2026". parse_date resolves a
    month to its LAST day, but rows stored before that convention hold the
    FIRST -- so the same article read as a 27-day forward jump and the gap rule
    would have minted a duplicate for it. Found by running the real 42 sources
    with --dry-run before committing (2026-09-16).
    """
    RA_URL = "https://www.syzygyassetmanagement.com/insights/articles/1107-should-trend-follow-carry"
    RA_STORED = [{"id": fa.article_id("research-affiliates", RA_URL),
                  "source_id": "research-affiliates", "url": RA_URL,
                  "title": "Should Trend Follow Carry", "date": "2026-02-01",
                  "date_raw": "FEB 2026"}]

    def _run_ra(self, monkeypatch, listing):
        monkeypatch.setitem(fa.FETCHERS, "research-affiliates", lambda s: [dict(r) for r in listing])
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        src = dict(SRC, id="research-affiliates")
        return fa.fetch_source(src, {r["id"] for r in self.RA_STORED}, dry_run=True,
                               existing_keys=fa.title_date_keys(self.RA_STORED),
                               existing_rows=self.RA_STORED)

    def test_a_re_normalised_month_is_not_a_new_issue(self, monkeypatch):
        got = self._run_ra(monkeypatch, [{"title": "Should Trend Follow Carry", "url": self.RA_URL,
                                          "date": "2026-02-28", "date_raw": "FEB 2026"}])
        assert got == []

    def test_the_next_month_still_is_a_new_issue(self, monkeypatch):
        got = self._run_ra(monkeypatch, [{"title": "Should Trend Follow Carry", "url": self.RA_URL,
                                          "date": "2026-03-31", "date_raw": "MAR 2026"}])
        assert len(got) == 1

    def test_an_old_row_with_no_raw_date_falls_back_on_the_month_edge(self, monkeypatch):
        """Rows stored before date_raw was kept: a 1st or a month-end is far
        more likely a normalised month than a real publication day."""
        stored = [dict(self.RA_STORED[0])]
        stored[0].pop("date_raw")
        monkeypatch.setitem(fa.FETCHERS, "research-affiliates",
                            lambda s: [{"title": "Should Trend Follow Carry", "url": self.RA_URL,
                                        "date": "2026-02-28"}])
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        got = fa.fetch_source(dict(SRC, id="research-affiliates"), {stored[0]["id"]}, dry_run=True,
                              existing_keys=fa.title_date_keys(stored), existing_rows=stored)
        assert got == []


def test_an_undated_row_at_a_reused_url_is_still_skipped(monkeypatch):
    """Without a date there is no issue key to store it under."""
    assert _run(monkeypatch, [{"title": "Weekly Market Update", "url": URL, "date": None}]) == []
