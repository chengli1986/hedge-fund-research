"""Franklin Templeton: list bodies and URLs reused for every issue (2026-09-14).

Bodies. Franklin posts put their key points in <li>: "Quick Thoughts: Mega
IPOs" has seven takeaway bullets and only an intro paragraph in <p>. With
"main p" four articles were stored as intro + disclosures and declined. One
page nests a list inside a list item, so only list items with no nested <p>
or <li> are taken -- otherwise the outer item repeats the inner text.

Reused URLs. A series card links to the series page
(/articles/series/from-the-market-desk), and some monthly posts reuse one
path (/articles/2026/fixed-income/global-macro-insights). article_id hashes
the URL, so every later issue looked already stored and was dropped. On
2026-09-14 the listing carried three such issues that were never ingested:
"From the US Market Desk: From Missouri" (09-14), "Global Macro Insights:
August 2026" (09-09), "Allocation Views: Bubbles, Bifurcation..." (09-07).
"""
from unittest.mock import MagicMock

import fetch_articles as fa
import fetch_content as fc

URL = "https://www.franklintempletonglobal.com/articles/series/from-the-market-desk"


class TestIssueOfAReusedUrl:
    STORED = [{"id": fa.article_id("franklin-templeton", URL), "source_id": "franklin-templeton",
               "title": "From the US Market Desk: Earnings Strong. Bond Yields a Risk",
               "date": "2026-07-27", "url": URL}]

    def _run(self, monkeypatch, listed, keys=True):
        sid = "franklin-templeton"
        monkeypatch.setitem(fa.FETCHERS, sid, lambda s: listed)
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        src = {"id": sid, "name": sid, "short_name": "Franklin", "method": "playwright",
               "url": "https://x/", "expected_hostname": ""}
        rows = self.STORED
        return fa.fetch_source(src, {r["id"] for r in rows},
                               existing_keys=fa.title_date_keys(rows) if keys else {},
                               existing_rows=rows)

    def test_a_new_issue_at_a_stored_url_is_stored(self, monkeypatch):
        got = self._run(monkeypatch, [{"title": "From the US Market Desk: From Missouri",
                                       "url": URL, "date": "2026-09-14"}])
        assert len(got) == 1
        assert got[0]["url"] == URL, "the content fetcher needs the real URL"
        assert got[0]["id"] != self.STORED[0]["id"]
        assert got[0]["id"] == fa.article_id("franklin-templeton", URL, issue_date="2026-09-14")

    def test_the_same_issue_seen_again_is_not_stored_twice(self, monkeypatch):
        issue = {"title": "From the US Market Desk: From Missouri", "url": URL, "date": "2026-09-14"}
        first = self._run(monkeypatch, [issue])
        rows = self.STORED + first
        monkeypatch.setattr(self, "STORED", rows)
        assert self._run(monkeypatch, [issue]) == []
        # The issue id alone must hold it, not only the title+date index.
        assert self._run(monkeypatch, [issue], keys=False) == []

    def test_an_edited_title_on_the_same_date_is_the_same_article(self, monkeypatch):
        got = self._run(monkeypatch, [{"title": "From the US Market Desk: Earnings Strong, Yields a Risk",
                                       "url": URL, "date": "2026-07-27"}])
        assert got == []

    def test_the_same_title_with_a_corrected_date_is_the_same_article(self, monkeypatch):
        got = self._run(monkeypatch, [{"title": self.STORED[0]["title"], "url": URL, "date": "2026-07-28"}])
        assert got == []

    def test_an_undated_listing_at_a_stored_url_is_skipped(self, monkeypatch):
        got = self._run(monkeypatch, [{"title": "Something else", "url": URL, "date": None}])
        assert got == []

    def test_main_passes_the_stored_rows(self):
        import inspect
        assert "existing_rows=" in inspect.getsource(fa.main)


class TestFranklinBody:
    def _run(self, tmp_path, monkeypatch, html):
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        import fetch_articles
        monkeypatch.setattr(fetch_articles, "_get_playwright_page", lambda *a, **k: html)
        fc.drain_extraction_paths()
        out = fc._fetch_content_franklin_templeton({"id": "ft-1", "url": URL})
        assert fc.drain_extraction_paths() == ["primary"]
        return out[0].read_text()

    def test_key_points_in_list_items_are_body(self, tmp_path, monkeypatch):
        html = ("<main><p>Originally published in Stephen Dover's LinkedIn Newsletter.</p>"
                "<ul><li><strong>The AI boom is reshaping capital markets.</strong> Private markets are where "
                "much of that story begins.</li><li>The IPO market is no longer waiting for a catalyst.</li></ul>"
                "<p>WHAT ARE THE RISKS? All investments involve risks.</p></main>")
        text = self._run(tmp_path, monkeypatch, html)
        assert "AI boom is reshaping capital markets" in text and "no longer waiting for a catalyst" in text
        assert text.index("LinkedIn") < text.index("AI boom") < text.index("WHAT ARE THE RISKS")

    def test_a_nested_list_is_not_repeated(self, tmp_path, monkeypatch):
        html = ("<main><p>" + "Intro to bewildering bonds. " * 5 + "</p><ul><li>So what do bond moves mean?"
                "<ul><li>Extend duration.</li><li>Position for broadening.</li></ul></li></ul></main>")
        text = self._run(tmp_path, monkeypatch, html)
        assert text.count("Extend duration.") == 1
        assert text.count("Position for broadening.") == 1

    def test_a_paragraph_inside_a_list_item_is_not_repeated(self, tmp_path, monkeypatch):
        html = ("<main><p>" + "Intro paragraph. " * 8 + "</p><ul><li><p>Point inside a paragraph.</p></li></ul></main>")
        text = self._run(tmp_path, monkeypatch, html)
        assert text.count("Point inside a paragraph.") == 1


class TestFranklinCompletePdf:
    """PDF lead pages (2026-10-24). The Animoca tokenization series (10-08)
    renders only a series introduction, "Read this article" cards and the
    disclosure in <main>; the article is behind "Download complete PDF"
    (franklintempletonprod.widen.net). All three parts were declined. Decided
    with the user: whenever that button is there and the PDF opens with the
    title, read the PDF (2 of 6 ordinary posts sampled carry it too, and their
    PDF is the complete report: US Treasuries 63K chars vs 13.7K on the page).
    """
    TITLE = "Article 1: Tokenization, RWAs, and the Future of Finance"
    PDF = "https://franklintempletonprod.widen.net/content/2tiga75xfy/pdf/animoca-part-one.pdf"
    OTHER = "https://franklintempletonprod.widen.net/s/nsxhkzpcmt/animoca-part-four-bridging-the-gap.pdf"
    PDF_TEXT = "Tokenization, RWAs, and the Future of Finance\n" + "Tokenized real-world assets change settlement. " * 60

    def _page(self, button=True):
        return ("<main><p>" + "This is article 1 of a 4-part series co-authored by Franklin Templeton. " * 4 + "</p>"
                + (f'<a href="{self.PDF}">Download complete PDF</a>' if button else "")
                + f'<a href="{self.OTHER}">Download this article</a>'
                + "<p>" + "DISCLOSURE This report is for informational purposes only. " * 6 + "</p></main>")

    def _run(self, tmp_path, monkeypatch, html, pdf_text=None, status=200):
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        import fetch_articles
        monkeypatch.setattr(fetch_articles, "_get_playwright_page", lambda *a, **k: html)
        asked = []
        monkeypatch.setattr(fc.requests, "get", lambda u, **k: (asked.append(u), MagicMock(
            status_code=status, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7 " + b"x" * 20000))[1])
        monkeypatch.setattr(fc, "_pdf_text", lambda content: pdf_text or self.PDF_TEXT)
        fc.drain_extraction_paths()
        out = fc._fetch_content_franklin_templeton({"id": "ft-2", "url": URL, "title": self.TITLE})
        return out[0].read_text(), asked, fc.drain_extraction_paths()

    def test_the_complete_pdf_replaces_the_lead_page(self, tmp_path, monkeypatch):
        text, asked, paths = self._run(tmp_path, monkeypatch, self._page())
        assert asked == [self.PDF]
        assert "Tokenized real-world assets change settlement" in text and "4-part series" not in text
        assert paths == ["primary", "linked_pdf"]

    def test_no_button_reads_the_page_and_never_another_parts_pdf(self, tmp_path, monkeypatch):
        text, asked, paths = self._run(tmp_path, monkeypatch, self._page(button=False))
        assert asked == [] and "4-part series" in text and paths == ["primary"]

    def test_a_pdf_that_is_not_this_article_or_fails_keeps_the_page(self, tmp_path, monkeypatch):
        for pdf_text, status in (("Quarterly factsheet for an unrelated fund. " * 40, 200), (None, 404)):
            text, asked, _ = self._run(tmp_path, monkeypatch, self._page(), pdf_text=pdf_text, status=status)
            assert asked == [self.PDF] and "4-part series" in text
