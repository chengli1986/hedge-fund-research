"""The health probe must exercise what the nightly run exercises.

Since the listing templates went in, 19 of the 42 sources fetch through
listing_templates.fetch while the probe called fetch_articles.FETCHERS[id]
directly -- the hand-written function the nightly run no longer executes.
Both directions of that split are wrong: a broken template would probe green
while production ingested nothing, and a hand-written fetcher rotting
unnoticed would FAIL the probe for a source that is actually fine. Both ends
now go through fetch_articles.listing_fetcher().
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gfh_probe", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_probe"] = gfh
spec.loader.exec_module(gfh)

import fetch_articles
import listing_templates

TEMPLATE_SOURCE = {
    "id": "probe-src", "name": "P", "short_name": "P", "method": "ssr",
    "url": "https://site.test/i", "expected_hostname": "site.test",
    "listing_template": {"type": "card_list", "fetch": "requests", "card": "div.card",
                         "link": "a"},
    "listing_template_active": True,
}
BESPOKE_SOURCE = {k: v for k, v in TEMPLATE_SOURCE.items()
                  if k not in ("listing_template", "listing_template_active")}


class TestListingFetcherSelection:
    def test_an_active_template_selects_the_template(self):
        assert fetch_articles.listing_fetcher(TEMPLATE_SOURCE) is listing_templates.fetch

    def test_without_the_flag_the_hand_written_function_is_selected(self, monkeypatch):
        monkeypatch.setitem(fetch_articles.FETCHERS, "probe-src", lambda s: [])
        assert fetch_articles.listing_fetcher(BESPOKE_SOURCE) is not listing_templates.fetch

    def test_a_spec_without_the_flag_does_not_switch(self, monkeypatch):
        """A spec alone changes nothing -- the A/B gate is what switches it."""
        monkeypatch.setitem(fetch_articles.FETCHERS, "probe-src", lambda s: [])
        source = dict(TEMPLATE_SOURCE, listing_template_active=False)
        assert fetch_articles.listing_fetcher(source) is not listing_templates.fetch

    def test_an_unregistered_source_selects_nothing(self):
        assert fetch_articles.listing_fetcher({"id": "not-a-source"}) is None


class TestProbeUsesIt:
    def _probe(self, monkeypatch, source, template_rows, bespoke_rows):
        monkeypatch.setattr(listing_templates, "fetch", lambda s: list(template_rows))
        monkeypatch.setitem(fetch_articles.FETCHERS, "probe-src", lambda s: list(bespoke_rows))
        import fetch_content
        # The real contract: a content fetcher hands back (path, status).
        monkeypatch.setitem(fetch_content.CONTENT_FETCHERS, "probe-src",
                            lambda a: ("/tmp/probe-src.txt", "ok"))
        return gfh.probe_source(source)

    def test_a_template_source_is_probed_through_the_template(self, monkeypatch):
        """The hand-written function returns rows; the template returns none.

        A probe that still called the hand-written function would report OK.
        """
        result = self._probe(monkeypatch, TEMPLATE_SOURCE, template_rows=[],
                             bespoke_rows=[{"title": "T", "url": "https://site.test/a",
                                            "date": "2026-09-26"}])
        assert result["articles_count"] == 0
        assert result["status"] in ("WARN", "FAIL")

    def test_a_hand_written_source_is_still_probed_through_its_function(self, monkeypatch):
        result = self._probe(monkeypatch, BESPOKE_SOURCE, template_rows=[],
                             bespoke_rows=[{"title": "T", "url": "https://site.test/a",
                                            "date": "2026-09-26"}])
        assert result["articles_count"] == 1
