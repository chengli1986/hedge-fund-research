"""Lazard Asset Management: duplicate rows and list-shaped bodies (2026-09-14).

Duplicates. The insights index intermittently renders card hrefs as AEM
repository paths -- /content/lam/us/en_us/...html -- instead of the mapped
/us/en_us/... form. Both open the same page (same 137,492 bytes, checked
live). article_id hashes the URL, so each such night re-stored the whole
visible batch as new articles: 10 on 2026-08-06 and 5 on 2026-08-26, 15
duplicate rows in all, some summarised twice and some declined once and
summarised once.

Bodies. "Behind the Headlines" is a weekly list of bullet points in
.cmp-text <ul><li>. The content selector took only .cmp-text p, so every
issue was stored as the series intro plus the disclaimer, and stage 3 rightly
declined 26 of them.
"""
from unittest.mock import MagicMock

import fetch_articles as fa
import fetch_content as fc

SOURCE = {"id": "lazard-am", "url": "https://www.lazardassetmanagement.com/us/en_us/research-insights",
          "expected_hostname": "lazardassetmanagement.com", "max_articles": 10}


def _card(href, title, date="Jun 26 2026"):
    return (f'<a class="cmp-insights-card__link" href="{href}" aria-label="{title}">'
            f'<div class="cmp-insights-card__container" data-date="{date}"></div></a>')


def _listing(monkeypatch, cards):
    resp = MagicMock(status_code=200, text="<html><body>" + "".join(cards) + "</body></html>")
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    return fa.fetch_lazard_am(SOURCE)


CLEAN = "https://www.lazardassetmanagement.com/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026"


class TestLazardUrlsAreCanonical:
    def test_aem_repository_path_maps_to_the_public_url(self, monkeypatch):
        got = _listing(monkeypatch, [_card(
            "/content/lam/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026.html",
            "Ceasefire Remains Fragile")])
        assert [a["url"] for a in got] == [CLEAN]

    def test_absolute_aem_path_maps_too(self, monkeypatch):
        got = _listing(monkeypatch, [_card(
            "https://www.lazardassetmanagement.com/content/lam/us/en_us/research-insights/"
            "market-insights/behind-the-headlines/june-26-2026.html", "Ceasefire Remains Fragile")])
        assert [a["url"] for a in got] == [CLEAN]

    def test_a_public_url_is_unchanged(self, monkeypatch):
        got = _listing(monkeypatch, [_card("/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026",
                                           "Ceasefire Remains Fragile")])
        assert [a["url"] for a in got] == [CLEAN]

    def test_both_forms_on_one_page_yield_one_article(self, monkeypatch):
        got = _listing(monkeypatch, [
            _card("/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026", "Ceasefire Remains Fragile"),
            _card("/content/lam/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026.html",
                  "Ceasefire Remains Fragile")])
        assert len(got) == 1

    def test_the_id_matches_the_row_already_stored(self, monkeypatch):
        """The point of canonicalising: the next fetch recognises the article."""
        got = _listing(monkeypatch, [_card(
            "/content/lam/us/en_us/research-insights/market-insights/behind-the-headlines/june-26-2026.html",
            "Ceasefire Remains Fragile")])
        assert fa.article_id("lazard-am", got[0]["url"]) == fa.article_id("lazard-am", CLEAN)


class TestBehindTheHeadlinesBody:
    def test_bullet_points_are_part_of_the_body(self, tmp_path, monkeypatch):
        html = """<html><body>
          <div class="cmp-text"><p><span>Each week, I provide my views on the global macroeconomic environment.
            This week's highlights include:</span></p>
            <ul><li><span class="article-body">The US-Iran conflict escalated, pushing Brent crude oil prices to nearly $110 per barrel.</span></li>
                <li><span class="article-body">US CPI inflation was largely in line with expectations.</span></li></ul></div>
          <div class="cmp-text"><p>Important Information Published on 11 September 2026.</p></div>
        </body></html>"""
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        resp = MagicMock(status_code=200, text=html); resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        fc.drain_extraction_paths()
        out = fc._fetch_content_lazard_am({"id": "lz-1", "url": CLEAN})
        assert fc.drain_extraction_paths() == ["primary"]
        text = out[0].read_text()
        assert "Brent crude oil prices to nearly $110" in text
        assert "US CPI inflation was largely in line" in text
        # document order: intro, then bullets, then the disclaimer
        assert text.index("highlights include") < text.index("Brent") < text.index("Important Information")

    def test_each_bullet_is_its_own_line(self, tmp_path, monkeypatch):
        html = ('<div class="cmp-text"><p>' + "Intro sentence for the week. " * 5 + '</p>'
                '<ul><li>First bullet.</li><li>Second bullet.</li></ul></div>')
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        resp = MagicMock(status_code=200, text=html); resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        text = fc._fetch_content_lazard_am({"id": "lz-2", "url": CLEAN})[0].read_text()
        assert "First bullet.\nSecond bullet." in text
