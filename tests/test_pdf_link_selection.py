"""Which PDF a page's content fetcher downloads (oaktree, gmo), 2026-09-14.

Both fetchers stored a different document than the article, and stage 3
faithfully summarised it -- so the summary on the page described B under A's
title, which check_grounding cannot see.

oaktree. _fetch_content_oaktree found the English PDF in the openPDF(...)
calls, then an `else` branch overwrote it with the first .pdf href anywhere on
the page. Howard Marks's memos link his earlier memos in the text, so
"Cockroaches in the Coal Mine" and "What's Going on in Private Credit?" were
both stored as "What Does the Market Know?", and "AI Hurtles Ahead" as
"Something of Value". A video page with no openPDF got the Form CRS. A live
survey of all 16 pages: every page with a PDF lists it through openPDF, so the
href fallback served no page correctly and is gone.

gmo. _fetch_content_gmo took the first .pdf href on the page. On a video page
that is the site-wide "Important Health Care Coverage" 1095-C form in the nav
menu. Site-wide nav/header/footer links are now excluded; the article's
download button (.share-links a.downloadDocumentTracking) wins; otherwise the
first remaining link (7-year forecast and EMD valuation pages have no button
and link the PDF in the text -- 9 of 36 pages).
"""
from unittest.mock import MagicMock

import fetch_content as fc

OAK = "https://www.oaktreecapital.com"


def _openpdf(title, url):
    return f"<a onclick=\"openPDF('{title}', '{url}')\">{title}</a>"


class TestOaktreePdfUrl:
    ENGLISH = f"{OAK}/docs/default-source/memos/cockroaches-in-the-coal-mine.pdf?sfvrsn=ec432966_2"
    PAGE = ("<html><body>"
            + _openpdf("Cockroaches in the Coal Mine_JPN", f"{OAK}/docs/translated-memos/cockroaches_jpn.pdf")
            + _openpdf("Cockroaches in the Coal Mine_SC", f"{OAK}/docs/translated-memos/cockroaches_sc.pdf")
            + _openpdf("Cockroaches in the Coal Mine", ENGLISH)
            + '<p>As I wrote in <a href="/docs/default-source/memos/what-does-the-market-know.pdf?sfvrsn=cb7a0165_10">'
              "What Does the Market Know?</a></p></body></html>")

    def test_the_english_openpdf_wins_over_a_memo_linked_in_the_text(self):
        assert fc._oaktree_pdf_url(self.PAGE, f"{OAK}/insights/memo/cockroaches-in-the-coal-mine") == self.ENGLISH

    def test_a_page_without_openpdf_yields_nothing(self):
        """The video page: no openPDF; any .pdf href is footer or unrelated."""
        page = '<html><body><footer><a href="/docs/form-crs.pdf">Form CRS</a></footer></body></html>'
        assert fc._oaktree_pdf_url(page, f"{OAK}/insights/insight-video/x") is None

    def test_only_translations_fall_back_to_the_first(self):
        page = _openpdf("Memo_JPN", f"{OAK}/docs/memo_jpn.pdf")
        assert fc._oaktree_pdf_url(page, f"{OAK}/insights/memo/m") == f"{OAK}/docs/memo_jpn.pdf"

    def test_a_relative_openpdf_url_is_made_absolute(self):
        page = _openpdf("Memo", "/docs/default-source/memos/memo.pdf")
        assert fc._oaktree_pdf_url(page, f"{OAK}/insights/memo/m") == f"{OAK}/docs/default-source/memos/memo.pdf"

    def test_the_fetcher_downloads_that_url(self, tmp_path, monkeypatch):
        """Wiring: the helper is useless if the fetcher still picks its own."""
        from test_unit_fetch_content import _fake_playwright
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        _fake_playwright(monkeypatch, self.PAGE)
        asked = []

        def fake_get(url, **k):
            asked.append(url)
            return MagicMock(status_code=404, headers={"Content-Type": "text/html"}, content=b"")

        monkeypatch.setattr(fc.requests, "get", fake_get)
        fc._fetch_content_oaktree({"id": "oak-1", "url": f"{OAK}/insights/memo/cockroaches-in-the-coal-mine"})
        assert asked == [self.ENGLISH]


GMO = "https://www.gmo.com"
HR_FORM = '<nav class="menu condensed"><ul><li><a href="/globalassets/gmo_affordable-care-act-1095-c-forms.pdf">Important Health Care Coverage</a></li></ul></nav>'


class TestGmoPdfUrl:
    def test_the_share_bar_download_button_wins(self):
        # The in-text link comes first in the document, so "first link" and
        # "button first" give different answers (the first version of this
        # test put the button first and could not tell them apart).
        page = (f'<main><p><a href="/globalassets/other-paper.pdf">see also</a></p>'
                f'<div class="share-links"><a class="share icon-pdf downloadDocumentTracking" '
                f'href="/globalassets/gmo_japan-equities.pdf">Download</a></div></main>{HR_FORM}')
        assert fc._gmo_pdf_url(page, f"{GMO}/x") == f"{GMO}/globalassets/gmo_japan-equities.pdf"

    def test_a_video_page_does_not_download_the_nav_menu_tax_form(self):
        page = f"<main><p>Watch the video.</p></main>{HR_FORM}"
        assert fc._gmo_pdf_url(page, f"{GMO}/x_video/") is None

    def test_a_forecast_page_without_a_button_uses_the_link_in_the_text(self):
        page = (f'{HR_FORM}<main><div class="container">As of July 31, 2026 '
                f'<a href="/globalassets/gmo-7-year-asset-class-forecastjul26.pdf">chart</a></div></main>')
        assert fc._gmo_pdf_url(page, f"{GMO}/x") == f"{GMO}/globalassets/gmo-7-year-asset-class-forecastjul26.pdf"

    def test_header_and_footer_links_are_excluded_too(self):
        page = ('<header><a href="/globalassets/brochure.pdf">b</a></header>'
                '<footer><a href="/globalassets/privacy.pdf">p</a></footer><main><p>No PDF.</p></main>')
        assert fc._gmo_pdf_url(page, f"{GMO}/x") is None

    def test_the_fetcher_downloads_that_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        page = f"<main><p>Watch the video.</p></main>{HR_FORM}"
        asked = []

        def fake_get(url, **k):
            asked.append(url)
            if len(asked) == 1:
                r = MagicMock(status_code=200, text=page); r.raise_for_status = lambda: None
                return r
            return MagicMock(status_code=404, headers={"Content-Type": "text/html"}, content=b"")

        monkeypatch.setattr(fc.requests, "get", fake_get)
        assert fc._fetch_content_gmo({"id": "gmo-1", "url": f"{GMO}/x_video/"}) is None
        assert asked == [f"{GMO}/x_video/"], "a PDF was downloaded for a page with no article PDF"


class TestGmoInTheNewsPage:
    """gmo 2026-09-23: research-library entries whose slug ends in `_inthenews`
    are press/podcast mentions (Jeremy Grantham on The Diary of a CEO): a
    speaker bio, a paragraph about the appearance, no article PDF. The fetcher
    returned None with "no article PDF on page" and the evidence had nothing
    else to say, so failure_labels fell through to body_too_short, a
    code-dependent label that keeps retrying. The fetcher knows what such a
    page is and says so through a failure hint."""

    def test_an_in_the_news_page_without_a_pdf_is_media_without_text(self, tmp_path, monkeypatch):
        import failure_labels as fl
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        page = ('<html><body><main><h1>Jeremy Grantham Joins The Diary of a CEO</h1>'
                '<p>GMO co-founder Jeremy Grantham sat down with Steven Bartlett.</p></main>'
                f'{HR_FORM}</body></html>')
        resp = MagicMock(status_code=200, text=page)
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        article = {"id": "gmo-inthenews", "url": f"{GMO}/americas/research-library/grantham-diary-of-a-ceo_inthenews/"}
        result, evidence = fc.fetch_with_evidence(article, fc._fetch_content_gmo)
        assert result is None
        assert fl.classify_content_failure(evidence)[0] == "media_without_text"

    def test_an_ordinary_page_without_a_pdf_keeps_the_generic_label(self, tmp_path, monkeypatch):
        import failure_labels as fl
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        page = f'<html><body><main><p>Some page.</p></main>{HR_FORM}</body></html>'
        resp = MagicMock(status_code=200, text=page)
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        article = {"id": "gmo-plain", "url": f"{GMO}/americas/research-library/some-paper/"}
        result, evidence = fc.fetch_with_evidence(article, fc._fetch_content_gmo)
        assert result is None
        assert fl.classify_content_failure(evidence)[0] == "body_too_short"
