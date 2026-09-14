"""An article already stored under another URL is not stored again (2026-09-14).

article_id hashes the URL, so a site that renames a slug after publication
hands us the same article as new. Twelve (source, title, date) groups were
stored twice: metlife, mfs, brookfield and rothschild renamed slugs;
man-group fixed "%20" to "-"; ares served a case variant; apollo lists one
piece under two sections; cohen-steers has a "-fp" edition; matthews a
transcript page beside the video page. Each showed twice on the page.

The URL is not a stable identity; title plus publication date is. A listed
article whose normalised title and date match a stored article of the same
source is skipped. Undated articles are not compared -- several sources
publish no dates, and a bare title repeats across years.

Matthews splits some titles across the card: h4.title "China Innovation:"
with the rest in the next <p>. Two different pieces both became "China
Innovation:", which this rule would have merged, so the colon case is
completed from that paragraph first.
"""
import json
from unittest.mock import MagicMock

import fetch_articles as fa


def _source(sid="metlife-im"):
    return {"id": sid, "name": sid, "short_name": sid, "method": "requests", "url": "https://x/",
            "expected_hostname": ""}


def _run(monkeypatch, stored, listed, sid="metlife-im", tmp_path=None):
    monkeypatch.setitem(fa.FETCHERS, sid, lambda s: listed)
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    keys = fa.title_date_keys(stored)
    ids = {r["id"] for r in stored}
    return fa.fetch_source(_source(sid), ids, existing_keys=keys)


STORED = [{"id": "a47468", "source_id": "metlife-im", "date": "2026-08-27",
           "title": "AI Investment and Pension Discount Rates: Emerging Risk for Plan Sponsors",
           "url": "https://www.metlife.com/investments/insights/ai-investment-and-pension-discount-rates/"}]


def test_a_renamed_slug_is_not_stored_again(monkeypatch):
    listed = [{"title": "AI Investment and Pension Discount Rates: Emerging Risk for Plan Sponsors",
               "url": "https://www.metlife.com/investments/insights/ai-investment-pension-discount-rates/",
               "date": "2026-08-27"}]
    assert _run(monkeypatch, STORED, listed) == []


def test_title_normalisation_ignores_case_quotes_and_punctuation(monkeypatch):
    listed = [{"title": "AI investment and pension discount rates — emerging risk for plan sponsors ",
               "url": "https://www.metlife.com/other", "date": "2026-08-27"}]
    assert _run(monkeypatch, STORED, listed) == []


def test_same_title_on_another_date_is_a_new_article(monkeypatch):
    listed = [{"title": STORED[0]["title"], "url": "https://www.metlife.com/other", "date": "2026-11-27"}]
    assert len(_run(monkeypatch, STORED, listed)) == 1


def test_undated_articles_are_not_compared(monkeypatch):
    stored = [dict(STORED[0], date=None)]
    listed = [{"title": STORED[0]["title"], "url": "https://www.metlife.com/other", "date": None}]
    assert len(_run(monkeypatch, stored, listed)) == 1


def test_undated_rows_are_not_indexed():
    """Both halves hold the rule: fetch_source also skips undated listings, so
    the index is checked directly (a mutation indexing undated rows survived
    the behavioural test above)."""
    assert fa.title_date_keys([dict(STORED[0], date=None)]) == {}
    assert fa.title_date_keys([dict(STORED[0], title="")]) == {}


def test_another_source_with_the_same_title_is_new(monkeypatch):
    listed = [{"title": STORED[0]["title"], "url": "https://www.mfs.com/x", "date": "2026-08-27"}]
    assert len(_run(monkeypatch, STORED, listed, sid="mfs-investment-management")) == 1


def test_two_spellings_in_one_listing_store_one(monkeypatch):
    listed = [{"title": "Hybrid: Rethinking Risk and Return", "date": "2026-07-15",
               "url": "https://www.apollo.com/insights/the-view-from-apollo/2026/07/hybrid"},
              {"title": "Hybrid: Rethinking Risk and Return", "date": "2026-07-15",
               "url": "https://www.apollo.com/insights/podcast/the-allocation/2026/07/hybrid"}]
    got = _run(monkeypatch, [], listed, sid="apollo-global-management")
    assert [a["url"] for a in got] == [listed[0]["url"]]


class TestTitleOnlySources:
    """Cohen & Steers republishes a piece per audience on later dates --
    "the-case-for-real-assets" (07-30), "-fp", "-inst" (08-21);
    "the-active-advantage-in-real-assets" (08-10) and "-global" (08-19). Same
    title, different date and URL, near-identical text: title+date cannot see
    it. Stripping the suffix from the URL could point the page at a URL that
    does not exist for that edition, so the source opts into title-only
    matching instead (no cohen-steers title recurs across different pieces)."""

    STORED = [{"id": "720002bd", "source_id": "cohen-steers", "date": "2026-07-30",
               "title": "The case for real assets",
               "url": "https://www.cohenandsteers.com/insights/the-case-for-real-assets/"}]
    LISTED = [{"title": "The case for real assets", "date": "2026-08-21",
               "url": "https://www.cohenandsteers.com/insights/the-case-for-real-assets-inst/"}]

    def _run(self, monkeypatch, title_only):
        sid = "cohen-steers"
        monkeypatch.setitem(fa.FETCHERS, sid, lambda s: self.LISTED)
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        src = dict(_source(sid), dedupe_on_title=title_only)
        keys = fa.title_date_keys(self.STORED, title_only_sources={sid} if title_only else set())
        return fa.fetch_source(src, {"720002bd"}, existing_keys=keys)

    def test_an_audience_edition_on_a_later_date_is_not_stored_again(self, monkeypatch):
        assert self._run(monkeypatch, title_only=True) == []

    def test_other_sources_still_require_the_date(self, monkeypatch):
        assert len(self._run(monkeypatch, title_only=False)) == 1

    def test_the_flag_is_set_for_cohen_steers_only(self):
        sources = json.load(open(fa.CONFIG_FILE))["sources"]
        assert [s["id"] for s in sources if s.get("dedupe_on_title")] == ["cohen-steers"]

    def test_main_passes_the_title_only_sources(self):
        import inspect
        assert "title_only_sources=" in inspect.getsource(fa.main)


def test_main_loads_and_passes_the_keys():
    import inspect
    src = inspect.getsource(fa.main)
    assert "title_date_keys(" in src and "existing_keys=" in src


class TestMatthewsSplitTitle:
    def _listing(self, monkeypatch, cards):
        resp = MagicMock(status_code=200, text="<html><body>" + "".join(cards) + "</body></html>")
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
        src = {"id": "matthews-asia", "url": "https://www.matthewsasia.com/insights/",
               "expected_hostname": "matthewsasia.com", "max_articles": 10}
        return fa.fetch_matthews_asia(src)

    @staticmethod
    def _card(href, title, para, date="06/18/2026"):
        return (f'<a class="item" href="{href}"><div class="text"><p class="category">Perspective</p>'
                f'<h4 class="title">{title}</h4><p>{para}</p><small class="date">{date}</small></div></a>')

    def test_a_title_ending_in_a_colon_is_completed_from_the_next_paragraph(self, monkeypatch):
        got = self._listing(monkeypatch, [self._card(
            "/insights/china/2026/china-innovation-completing/", "China Innovation:",
            "Completing Global Innovation and Emerging Markets Equity Allocations")])
        assert got[0]["title"] == "China Innovation: Completing Global Innovation and Emerging Markets Equity Allocations"

    def test_a_complete_title_does_not_absorb_the_description(self, monkeypatch):
        got = self._listing(monkeypatch, [self._card(
            "/insights/japan/2026/outlook/", "The Japan Opportunity: Outlook",
            "Shuntaro Takeuchi discusses the team's outlook for Japanese equities.")])
        assert got[0]["title"] == "The Japan Opportunity: Outlook"
