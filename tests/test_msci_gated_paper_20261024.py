"""MSCI gated research papers (2026-10-24).

A /paper/ page whose button opens the "Unlock for free" form shows only a
preview, a form link and a disclaimer. The analysis step declined two such
pages ("The Anatomy of US Equities" 10-07, "Latest on US Commercial-Property
Pricing" 10-24). The page names the PDF it unlocks in its Next.js payload
(pdfDamPath under /content/dam/...); the form's "View PDF" link is the same
path under /downloads/. Decided with the user on 10-24: read that PDF.

The markup copies the shape of the 10-24 page (escaped flight payload, a
sidebar promoting another paper); live it gave 8,109 and 57,115 chars.
"""
from unittest.mock import MagicMock

import fetch_content as fc

URL = "https://www.msci.com/research-and-insights/paper/rca-commercial-property-price-indexes-rca-cppi"
TITLE = "Latest on US Commercial-Property Pricing"
DAM = "/content/dam/web/msci-com/research-and-insights/paper/rca-commercial-property-price-indexes-rca-cppi/2610_RCACPPI_US_MSCI.pdf"
OTHER = "/content/dam/web/msci-com/research-and-insights/paper/msci-real-estate-market-size/size.pdf"
PREVIEW = ("Latest on US Commercial-Property Pricing Research Paper October 22, 2026 Preview "
           + "MSCI's commercial-property price indexes provide a consistent measure. " * 3
           + "Read the full paper Provide your information for instant access to our research papers. "
           + "The content of this page is for informational purposes only. " * 4)
PDF_TEXT = "Q3 2026 RCA CPPI US 0.9% Change past year " + "U.S. commercial property prices rose in September. " * 40


def _payload(dam, handler="gated-pdf"):
    return ('<script>self.__next_f.push([1,"{\\"pdfDamPath\\":\\"' + dam
            + '\\",\\"formHandlerType\\":\\"' + handler + '\\"}"])</script>')


def _page(*payloads):
    return f"<html><body><main><p>{PREVIEW}</p></main>{''.join(payloads)}</body></html>"


def _run(tmp_path, monkeypatch, page, url=URL, pdf_status=200, form=None):
    """`form` is what the form fallback (msci_gate) returns; its calls land in `asked`."""
    import msci_gate

    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []
    monkeypatch.setattr(msci_gate, "fetch_gated_pdf",
                        lambda page_url, pdf_url, **k: (asked.append(("form", pdf_url)), form)[1])

    def fake_get(u, **k):
        asked.append(u)
        if u.endswith(".pdf"):
            return MagicMock(status_code=pdf_status, headers={"Content-Type": "application/pdf"},
                             content=b"%PDF-1.6 " + b"x" * 20000)
        r = MagicMock(status_code=200, text=page)
        r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    monkeypatch.setattr(fc, "_pdf_text", lambda content: PDF_TEXT)
    out = fc._fetch_content_msci({"id": "msci-1", "title": TITLE, "url": url})
    return (out[0].read_text() if out else None), asked


def test_a_gated_paper_is_read_from_its_own_pdf_under_downloads(tmp_path, monkeypatch):
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(OTHER, "gated-pdf"), _payload(DAM)))
    assert asked[-1] == "https://www.msci.com" + DAM.replace("/content/dam/", "/downloads/")
    assert text.startswith(TITLE + "\n\nQ3 2026 RCA CPPI US")
    assert "Provide your information" not in text


def test_only_another_papers_pdf_on_the_page_keeps_the_preview(tmp_path, monkeypatch):
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(OTHER)))
    assert not any(u.endswith(".pdf") for u in asked)
    assert "Provide your information" in text


def test_a_failed_pdf_download_and_a_failed_form_keep_the_preview(tmp_path, monkeypatch):
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM)), pdf_status=403, form=None)
    assert ("form", "https://www.msci.com" + DAM.replace("/content/dam/", "/downloads/")) in asked
    assert "Provide your information" in text and "RCA CPPI US 0.9%" not in text


def test_the_form_is_the_fallback_when_the_direct_pdf_is_refused(tmp_path, monkeypatch):
    """10-24: 22 papers' /downloads/ path 307s back to the page until the visitor unlocks."""
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM)), pdf_status=307,
                       form=b"%PDF-1.6 " + b"y" * 20000)
    assert text.startswith(TITLE + "\n\nQ3 2026 RCA CPPI US")
    assert [a for a in asked if isinstance(a, tuple)] == [("form", asked[-1][1])]


def test_the_form_is_not_tried_when_the_direct_pdf_works(tmp_path, monkeypatch):
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM)), form=b"%PDF-1.6 never")
    assert not any(isinstance(a, tuple) for a in asked)
    assert "RCA CPPI US 0.9%" in text


def test_the_form_is_not_tried_for_an_ambiguous_or_foreign_pdf(tmp_path, monkeypatch):
    second = DAM.replace("2610_RCACPPI_US_MSCI.pdf", "2609_RCACPPI_US_MSCI.pdf")
    for page in (_page(_payload(OTHER)), _page(_payload(DAM), _payload(second))):
        _, asked = _run(tmp_path, monkeypatch, page, pdf_status=307, form=b"%PDF-1.6 " + b"y" * 20000)
        assert not any(isinstance(a, tuple) for a in asked)


def test_a_page_that_is_not_gated_is_left_alone(tmp_path, monkeypatch):
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM, "subscription")))
    assert not any(u.endswith(".pdf") for u in asked)
    blog = URL.replace("/paper/", "/blog-post/")
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM)), url=blog)
    assert not any(u.endswith(".pdf") for u in asked)


def test_two_pdfs_in_the_papers_own_folder_is_ambiguous_and_keeps_the_preview(tmp_path, monkeypatch):
    second = DAM.replace("2610_RCACPPI_US_MSCI.pdf", "2609_RCACPPI_US_MSCI.pdf")
    text, asked = _run(tmp_path, monkeypatch, _page(_payload(DAM), _payload(second)))
    assert not any(u.endswith(".pdf") for u in asked)
    assert "Provide your information" in text
