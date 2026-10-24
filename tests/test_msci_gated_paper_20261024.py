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
import failure_labels

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


def test_a_failed_pdf_download_and_a_failed_form_store_nothing_and_retry(tmp_path, monkeypatch):
    """Review M2: storing the preview as "ok" meant the paper was never fetched
    again and the analysis step declined it (the 10-07 / 10-24 declines). A
    failure goes to the retry queue instead, labelled fetch_error."""
    def fetcher(article):
        out = _run(tmp_path, monkeypatch, _page(_payload(DAM)), pdf_status=307, form=None)
        fetcher.asked = out[1]
        return None if out[0] is None else (tmp_path / "msci-1.txt", "ok")
    outcome, evidence = fc.fetch_with_evidence({"id": "msci-1", "url": URL}, fetcher)
    assert outcome is None
    assert ("form", "https://www.msci.com" + DAM.replace("/content/dam/", "/downloads/")) in fetcher.asked
    assert not (tmp_path / "msci-1.txt").exists()
    assert failure_labels.classify_content_failure(evidence)[0] == "fetch_error"


def test_an_unreadable_or_short_gated_pdf_stores_nothing(tmp_path, monkeypatch):
    for pdf_text, label in ((None, "pdf_not_usable"), ("tiny", "body_too_short")):
        def fake_text(content, t=pdf_text):
            if t is None:
                raise ValueError("no xref")
            return t

        def fetcher(article):
            monkeypatch.setattr(fc, "_pdf_text", fake_text)
            import msci_gate
            monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
            monkeypatch.setattr(msci_gate, "fetch_gated_pdf", lambda *a, **k: None)
            monkeypatch.setattr(fc.requests, "get", lambda u, **k: MagicMock(
                status_code=200, headers={"Content-Type": "application/pdf"},
                content=b"%PDF-1.6 " + b"x" * 20000) if u.endswith(".pdf") else MagicMock(
                status_code=200, text=_page(_payload(DAM)), raise_for_status=lambda: None))
            return fc._fetch_content_msci({"id": "msci-2", "title": TITLE, "url": URL})
        outcome, evidence = fc.fetch_with_evidence({"id": "msci-2", "url": URL}, fetcher)
        assert outcome is None and not (tmp_path / "msci-2.txt").exists()
        assert failure_labels.classify_content_failure(evidence)[0] == label


def test_a_gated_button_in_the_sidebar_does_not_make_this_page_gated(tmp_path, monkeypatch):
    """L3: the lock type is read from the same button object as the PDF path
    (live 10-24: pdfDamPath, eventId, assetName, formHandlerType in one object)."""
    own_subscription = _payload(DAM, "subscription")
    sidebar_gated = _payload(OTHER, "gated-pdf")
    for page in (_page(sidebar_gated, own_subscription), _page(own_subscription, sidebar_gated)):
        text, asked = _run(tmp_path, monkeypatch, page, pdf_status=307, form=b"%PDF-1.6 " + b"y" * 20000)
        assert not any(isinstance(u, tuple) or u.endswith(".pdf") for u in asked)
        assert "Provide your information" in text


def test_the_form_module_is_part_of_the_msci_code_version(monkeypatch):
    """L5: a fix in msci_gate re-queues msci's code-dependent permafails, as a fix in the fetcher does."""
    import msci_gate
    art = {"source_id": "msci-research", "url": URL}
    before = fc.code_version_for(art)
    real = fc.inspect.getsource
    monkeypatch.setattr(fc.inspect, "getsource",
                        lambda o: real(o) + "# changed" if o is msci_gate else real(o))
    assert fc.code_version_for(art) != before


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


def test_the_recorded_paths_are_not_reported_as_drift(tmp_path, monkeypatch):
    """The nightly health probe and weekly audit flag paths outside the whitelist (review H1)."""
    for status, form in ((200, None), (307, b"%PDF-1.6 " + b"y" * 20000)):
        fc.drain_extraction_paths()
        text, _ = _run(tmp_path, monkeypatch, _page(_payload(DAM)), pdf_status=status, form=form)
        paths = fc.drain_extraction_paths()
        assert "RCA CPPI US 0.9%" in text
        assert paths[-1] in ("gated_pdf", "gated_pdf_form")
        assert failure_labels.unexpected_paths(paths) == [], paths
