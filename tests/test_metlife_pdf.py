"""MetLife IM: pages whose article is the "Download PDF" (2026-09-15).

Twelve metlife rows had no body. Two URLs now redirect to the site home (the
pages are gone). The other ten -- quarterly market reviews, the pension
funding status, a chartbook -- are a short lead plus a "Download PDF" (or
"Download the chartbook") button in <main>, and the PDF is the article:
10,892-75,987 chars each. The fetcher deliberately stored nothing for these
rather than the lead; it now reads the PDF.

The PDF must be recognisable as this article, but its opening does not repeat
the page title verbatim (9 of 10 would fail an exact match: "Equity Market
Review Q2 2026" for "Q2 2026 Equity Market Review", "Chart Book" for
"Chartbook", and the site's own "Pending Funding Status" for "Pension"). So:
the PDF behind a Download button only, and at least 75% of the title's words
in its first 2,000 chars (the ten measured 0.80-1.00).

Known limit: a Download button pointing at the previous quarter's PDF would
still share ~80% of the words. Taking only the page's own button is what
keeps that unlikely; the overlap check alone would not.
"""
from unittest.mock import MagicMock

import pytest

import fetch_content as fc

LEAD = "<p>Artificial intelligence remained the dominant market theme in the second quarter of 2026.</p>"


def _page(extra=""):
    return (f"<html><body><main>{LEAD}"
            "<div><a class='btn' href='/content/dam/metlifecom/us/investments/pdf/MIM-equityMarketReview-Q2-2026.pdf'>"
            f"Download PDF</a></div>{extra}</main></body></html>")


def _run(tmp_path, monkeypatch, page, pdf_text, title="Q2 2026 Equity Market Review"):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []

    def fake_get(url, **k):
        asked.append(url)
        if ".pdf" in url:
            return MagicMock(status_code=200, headers={"Content-Type": "application/pdf"},
                             content=b"%PDF-1.7 " + b"x" * 20000)
        r = MagicMock(status_code=200, text=page, url="https://www.metlife.com/investments/en-us/x/")
        r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc, "_pdf_text", lambda content: pdf_text)
    out = fc._fetch_content_metlife_im({"id": "ml-1", "title": title,
                                        "url": "https://www.metlife.com/investments/en-us/x/"})
    return out, asked


PDF = "EQUITIES Equity Market Review Q2 2026 Artificial Intelligence has become everything. " + "Detail. " * 200


def test_the_download_pdf_is_the_body_when_the_page_is_a_lead(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, _page(), PDF)
    assert out is not None and out[1] == "ok"
    assert "has become everything" in out[0].read_text()
    assert any("MIM-equityMarketReview-Q2-2026.pdf" in u for u in asked)


def test_a_pdf_that_is_not_this_article_is_not_used(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, _page(), "Private Credit Quarterly Review " * 50)
    assert out is None


def test_a_pdf_link_that_is_not_a_download_button_is_not_followed(tmp_path, monkeypatch):
    page = (f"<html><body><main>{LEAD}<div class='related'><a href='/x/other-report.pdf'>"
            "Our 2025 outlook</a></div></main></body></html>")
    out, asked = _run(tmp_path, monkeypatch, page, PDF)
    assert out is None and not any(".pdf" in u for u in asked)


def test_a_page_with_a_read_more_body_does_not_fetch_the_pdf(tmp_path, monkeypatch):
    body = "<div class='read-more-section richtext'>" + "<p>Full article paragraph here.</p>" * 30 + "</div>"
    out, asked = _run(tmp_path, monkeypatch, _page(body), PDF)
    assert out is not None and not any(".pdf" in u for u in asked)


@pytest.mark.parametrize("title,head,expected", [
    ("Q2 2026 Equity Market Review", "EQUITIES Equity Market Review Q2 2026 AI", True),
    ("Q3 2026 European Commercial Real Estate Chartbook",
     "European Commercial Real Estate Chart Book Q3 2026", True),       # 0.86
    ("August 2026 Pending Funding Status", "FIXED INCOME | AUGUST 2026 Pension Funding Status", True),  # 0.80
    ("Q2 2026 Equity Market Review", "Matthews Asia Fund Factsheet", False),
    ("", "anything", False),
])
def test_title_recognition(title, head, expected):
    assert fc._pdf_matches_title(head, title) is expected
