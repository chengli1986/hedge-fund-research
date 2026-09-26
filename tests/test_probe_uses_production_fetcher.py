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


class TestAlertSaysHowToRollBack:
    """A listing failure on a template source must say so, and only that one.

    This repo's recurring defect is knowledge that is detected and never
    reaches a human. "aberdeen: fetch_articles returned 0 articles" sends
    whoever reads the 04:30 email into fetch_articles.py, where the function
    still exists and still works -- while what actually ran is a spec in
    config/sources.json and the one-flag rollback is mentioned nowhere.

    Only the listing step, though: content fetchers are never templated, so
    hinting at a template rollback on a content failure would point the
    reader at the wrong file.
    """

    def _probe(self, monkeypatch, tmp_path, source, rows, content_ok=True):
        body = tmp_path / "probe-src.txt"
        body.write_text("x" * 5000)
        monkeypatch.setattr(listing_templates, "fetch",
                            (lambda s: list(rows)) if rows is not None
                            else _raise)
        monkeypatch.setitem(fetch_articles.FETCHERS, "probe-src",
                            (lambda s: list(rows)) if rows is not None else _raise)
        import fetch_content
        monkeypatch.setitem(
            fetch_content.CONTENT_FETCHERS, "probe-src",
            (lambda a: (str(body), "ok")) if content_ok else (lambda a: None))
        return gfh.probe_source(source)

    def test_zero_articles_from_a_template_names_the_rollback_flag(self, monkeypatch, tmp_path):
        r = self._probe(monkeypatch, tmp_path, TEMPLATE_SOURCE, rows=[])
        assert "listing_template_active" in r["reason"] and "card_list" in r["reason"]

    def test_a_raising_template_names_the_rollback_flag(self, monkeypatch, tmp_path):
        r = self._probe(monkeypatch, tmp_path, TEMPLATE_SOURCE, rows=None)
        assert r["status"] == "FAIL" and "listing_template_active" in r["reason"]

    def test_a_hand_written_source_says_nothing_about_templates(self, monkeypatch, tmp_path):
        r = self._probe(monkeypatch, tmp_path, BESPOKE_SOURCE, rows=[])
        assert "listing_template" not in r["reason"]

    def test_a_content_failure_does_not_blame_the_template(self, monkeypatch, tmp_path):
        """The listing worked; the article page is what failed."""
        r = self._probe(monkeypatch, tmp_path, TEMPLATE_SOURCE,
                        rows=[{"title": "T", "url": "https://site.test/a",
                               "date": "2026-09-26"}], content_ok=False)
        assert r["status"] != "OK" and "listing_template" not in r["reason"]


def _raise(source):
    raise RuntimeError("selector gone")
