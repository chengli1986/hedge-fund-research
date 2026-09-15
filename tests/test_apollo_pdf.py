"""Apollo: a short page whose article is the "Download Whitepaper" PDF (2026-09-15).

Labelling the six apollo permafail rows from evidence left three as
body_too_short rather than media_without_text. They are not episodes: "2026
Midyear Credit Outlook" (60,308 chars), "Apollo Impact Mission: 2025 Annual
Impact Report" (61,731) and "Portfolio Allocations: Where Does Private IG
Credit Belong?" (24,581) are a short excerpt plus a download button. Apollo's
button is a custom element, <acl-apollo-button iconType="download"
href="....pdf">, which is why a[href*=".pdf"] found nothing.

The title check decides ownership: one convergence-series PDF sits behind
the buttons of both "Portfolio Allocations..." (matches) and "Finding
Opportunity in a Converging Credit Market" (does not), so only the first
takes it.
"""
from unittest.mock import MagicMock

import fetch_content as fc

TITLE = "2026 Midyear Credit Outlook: Adoption, Financing and Investing in the Age of AI"
PAGE = ("<html><body><div class='cmp-text'><p>Public markets are being stretched by record financing needs.</p></div>"
        "<acl-apollo-button analyticsId='button' iconType='download' "
        "href='/content/dam/apolloaem/documents/insights/2026/2026-midyear-credit-outlook.pdf'>Download Whitepaper"
        "</acl-apollo-button></body></html>")
PDF = "2026 Midyear Credit Outlook Adoption, Financing, and Investing in the Age of AI August 2026 " + "Credit. " * 400


def _run(tmp_path, monkeypatch, page, pdf_text, title=TITLE):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []

    def fake_get(url, **k):
        asked.append(url)
        if url.endswith(".pdf"):
            return MagicMock(status_code=200, headers={"Content-Type": "application/pdf"},
                             content=b"%PDF-1.7" + b"x" * 20000)
        r = MagicMock(status_code=200, text=page); r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc, "_pdf_text", lambda content: pdf_text)
    out = fc._fetch_content_apollo({"id": "ap-1", "title": title,
                                    "url": "https://www.apollo.com/insights-news/insights/2026/08/2026-midyear-credit-outlook"})
    return out, asked


def test_the_download_button_pdf_is_the_body(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, PAGE, PDF)
    assert out is not None and out[1] == "ok"
    assert "Adoption, Financing, and Investing" in out[0].read_text()
    assert asked[-1] == "https://www.apollo.com/content/dam/apolloaem/documents/insights/2026/2026-midyear-credit-outlook.pdf"


def test_a_pdf_belonging_to_another_title_is_not_used(tmp_path, monkeypatch):
    convergence = ("Convergence Fixed Income Replacement Newsletter April 2026 Portfolio Allocations: "
                   "Where Does Private IG Credit Belong? " + "Detail. " * 300)
    out, _ = _run(tmp_path, monkeypatch, PAGE, convergence,
                  title="Finding Opportunity in a Converging Credit Market")
    assert out is None


def test_an_episode_page_without_a_download_button_stays_skipped(tmp_path, monkeypatch):
    page = "<html><body><div class='cmp-text'><p>In our latest episode we discuss credit.</p></div></body></html>"
    out, asked = _run(tmp_path, monkeypatch, page, PDF, title="Investing Across the Credit Cycle")
    assert out is None and not any(u.endswith(".pdf") for u in asked)


def test_a_full_article_never_fetches_the_pdf(tmp_path, monkeypatch):
    page = PAGE.replace("<p>Public markets", "<p>" + "Long analysis paragraph. " * 80 + "Public markets")
    out, asked = _run(tmp_path, monkeypatch, page, PDF)
    assert out is not None and not any(u.endswith(".pdf") for u in asked)
