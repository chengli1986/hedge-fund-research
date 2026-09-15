"""Matthews Asia: a short page whose article is a linked PDF (2026-09-15).

Fourteen matthews rows were permafail. Probed live: thirteen are video
pages with a 200-420 char lead and no transcript or PDF -- skipping them is
the designed outcome (MATTHEWS_MIN_CONTENT). One is not: "China Innovation:
Completing Global Innovation and Emerging Markets Equity Allocations" is a
Perspective whose page holds only the lead and a "Read Now" link to
/siteassets/documents/pdf/china-innovation.pdf -- 11 pages, 39,238 chars,
opening with that exact title.

When the page text is below the minimum, the first PDF linked from the page
body is read, and used only if its opening contains the article title; any
other PDF (a factsheet, a brochure) leaves the page skipped as before.
"""
from unittest.mock import MagicMock

import fetch_content as fc

TITLE = "China Innovation: Completing Global Innovation and Emerging Markets Equity Allocations"
PAGE = ("<html><body><header><a href='/siteassets/documents/pdf/brochure.pdf'>Brochure</a></header>"
        "<main><p>China has evolved into a broad innovation ecosystem.</p>"
        "<div><a href='/siteassets/documents/pdf/china-innovation.pdf'>Read Now</a></div></main></body></html>")
PDF_TEXT = ("June 2026 " + TITLE + " Executive Summary For many global investors, China exposure is still "
            "framed narrowly. " + "Semiconductors, power generation and health care. " * 40)


def _run(tmp_path, monkeypatch, page, pdf_text):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []

    def fake_get(url, **k):
        asked.append(url)
        if url.endswith(".pdf"):
            return MagicMock(status_code=200, headers={"Content-Type": "application/pdf"},
                             content=b"%PDF-1.7 " + b"x" * 20000)
        r = MagicMock(status_code=200, text=page); r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc, "_pdf_text", lambda content: pdf_text)
    out = fc._fetch_content_matthews_asia({"id": "m-1", "title": TITLE,
                                           "url": "https://www.matthewsasia.com/insights/china/2026/x/"})
    return out, asked


def test_the_article_pdf_is_read_when_the_page_is_only_a_lead(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, PAGE, PDF_TEXT)
    assert out is not None and out[1] == "ok"
    assert "Executive Summary For many global investors" in out[0].read_text()
    assert asked[-1].endswith("/siteassets/documents/pdf/china-innovation.pdf"), "the header brochure was chosen"


def test_a_pdf_that_is_not_this_article_is_not_used(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, PAGE, "Matthews Asia Fund Factsheet " * 100)
    assert out is None


def test_a_video_page_without_a_pdf_is_still_skipped(tmp_path, monkeypatch):
    video = "<html><body><main><p>In this video, the portfolio manager discusses Japan.</p></main></body></html>"
    out, asked = _run(tmp_path, monkeypatch, video, PDF_TEXT)
    assert out is None and not any(u.endswith(".pdf") for u in asked)


def test_a_full_page_does_not_fetch_the_pdf(tmp_path, monkeypatch):
    page = PAGE.replace("<p>China has evolved", "<p>" + "Long article paragraph. " * 40 + "China has evolved")
    out, asked = _run(tmp_path, monkeypatch, page, PDF_TEXT)
    assert out is not None and not any(u.endswith(".pdf") for u in asked)
