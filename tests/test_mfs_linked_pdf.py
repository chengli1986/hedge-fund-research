"""MFS: a hub page whose report is the linked PDF (2026-11-07).

"Portfolio Perspectives // Q4 2026" (2026-11-03) was stored as a 517-char page
-- intro, author names, an "In this edition" list -- and Stage 3 declined it as
teaser_only. The report itself is the PDF behind the page's "Read Now" button
(mfsi_fly_3487552.pdf, 39,424 chars, opening "PORTFOLIO | PERSPECTIVES |
Practical applications for asset allocators // Q4 2026").

Measured on the 28 distinct live MFS pages 2026-11-07: article pages give
2,940-16,447 chars of `article p` (and most also carry a "Download" PDF of the
same piece, which is not needed); the Straight Talk webcast teasers give
376-719 with no PDF at all; the Portfolio Perspectives hub gives 515. So the
linked PDF is read only below MFS_HUB_BODY_MAX, only from a "Read Now" /
"Download" link inside <article>, and only if its opening matches the title.
Otherwise the page text is stored as before.
"""
from unittest.mock import MagicMock

import fetch_content as fc

URL = "https://www.mfs.com/en-us/investment-professional/insights/market-insights/portfolio-perspectives.html"
PDF_HREF = "/content/dam/mfs-enterprise/mfscom/insights/portfolio-perspectives/mfsi_fly_3487552.pdf"
TITLE = "Portfolio Perspectives // Q4 2026"
PDF_TEXT = ("PORTFOLIO | PERSPECTIVES | Practical applications for asset allocators // Q4 2026\n"
            + "Emerging market debt spreads have tightened as allocators rotate. " * 300)
HUB_INTRO = ("Dive into this quarter's Portfolio Perspectives for practical applications "
             "for asset allocators in today's complex market.")


def _hub_page(link_text="Read Now", href=PDF_HREF):
    return (
        "<html><body><main><article>"
        "<div class='hero'><div class='hero__content'>"
        f"<p>{HUB_INTRO}</p>"
        f"<a class='cta-primary' href='{href}' title='{TITLE}'>{link_text}</a></div></div>"
        "<p>Jonathan Hubbard, CFA</p><p>Managing Director, Strategy and Insights Group</p>"
        "<p>In this edition</p>"
        "<p>Emerging Market Debt: Navigating ESG Risk and Uncertainty</p>"
        "</article></main><footer><a href='/x/factsheet.pdf'>Fund factsheet</a></footer>"
        "</body></html>")


def _article_page():
    body = "".join(f"<p>{'High stock prices must be backed by strong fundamentals. ' * 12}</p>"
                   for _ in range(8))
    return ("<html><body><main><article>" + body +
            "<a href='/content/dam/mfs/x/mfse_fly_5145900.pdf'>Download</a>"
            "<a href='/content/dam/mfs/x/mfse_fly_5145900.pdf'>Print</a>"
            "</article></main></body></html>")


def _run(tmp_path, monkeypatch, page, pdf_text=PDF_TEXT, title=TITLE):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []

    def fake_get(url, **k):
        asked.append(url)
        if ".pdf" in url:
            return MagicMock(status_code=200, headers={"Content-Type": "application/pdf"},
                             content=b"%PDF-1.7" + b"x" * 5000)
        r = MagicMock(status_code=200, text=page, url=URL)
        r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc, "_pdf_text", lambda content: pdf_text)
    out = fc._fetch_content_mfs({"id": "mfs-hub", "title": title, "url": URL})
    return out, asked


def test_hub_page_stores_the_linked_pdf(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, _hub_page())
    assert out is not None and out[1] == "ok"
    text = out[0].read_text()
    assert text.startswith("PORTFOLIO | PERSPECTIVES")
    assert len(text) > 10_000
    assert asked == [URL, "https://www.mfs.com" + PDF_HREF]


def test_a_download_link_on_a_hub_page_is_also_read(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, _hub_page(link_text="Download"))
    assert out is not None and out[0].read_text().startswith("PORTFOLIO | PERSPECTIVES")


def test_a_pdf_that_is_not_this_article_is_not_stored(tmp_path, monkeypatch):
    """Opening does not match the title: the page text is kept, as before."""
    out, asked = _run(tmp_path, monkeypatch, _hub_page(),
                      pdf_text="MFS Global Equity Fund | Fact sheet | Q3 2026\n" + "x " * 3000)
    assert any(".pdf" in u for u in asked)
    assert out is not None
    text = out[0].read_text()
    assert "Fact sheet" not in text
    assert text.startswith("Dive into this quarter's Portfolio Perspectives")


def test_a_pdf_outside_the_article_or_without_a_read_link_is_not_fetched(tmp_path, monkeypatch):
    """The footer factsheet and a "Print" link are never taken as the report."""
    out, asked = _run(tmp_path, monkeypatch, _hub_page(link_text="Print"))
    assert not any(".pdf" in u for u in asked)
    assert out is not None and out[0].read_text().startswith("Dive into")


def test_a_full_article_page_is_unchanged_and_its_pdf_not_fetched(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, _article_page(),
                      title="High Stock Prices Must Be Backed by Strong Fundamentals")
    assert asked == [URL]
    assert out is not None and out[1] == "ok"
    assert out[0].read_text().startswith("High stock prices must be backed")


def test_a_teaser_without_any_pdf_is_stored_as_before(tmp_path, monkeypatch):
    """Straight Talk webcast teasers (376-719 chars, no PDF) keep today's behaviour."""
    page = ("<html><body><main><article><p>" + "Join host Jenine Garrelick for a discussion. " * 8
            + "</p></article></main></body></html>")
    out, asked = _run(tmp_path, monkeypatch, page, title="The Benchmark Blind Spot")
    assert asked == [URL]
    assert out is not None and out[0].read_text().startswith("Join host")
