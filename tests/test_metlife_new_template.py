"""MetLife IM: articles on the new page template (2026-11-07).

"What the U.S. Midterm Election Results Could Mean for Markets" (2026-11-07)
was labelled selector_miss: the page has no `div.read-more-section`, the body
sits in `main div.responsive-columns div.richtext.body-b1` (4 paragraphs,
2,578 chars). Articles from 2026-10-29..11-03 still carry the read-more
section (10-39 paragraphs each), so this is a second template for new pieces,
not a site redesign.

The extractor kept no `main p` fallback on purpose: PDF-led pages (quarterly
reviews, pension funding status, chartbooks) have a richtext lead of 267-579
chars (measured on five live pages 2026-11-07) in front of a "Download PDF"
button, and storing that lead would summarise a blurb as the research. So the
new-template body is read only when the page has no Download PDF button, the
body is at least METLIFE_TEMPLATE_BODY_MIN chars, and the request was not
redirected (dead pinebridge URLs land on the home page, 1,024 chars of cards).
"""
from unittest.mock import MagicMock

import fetch_content as fc

URL = "https://www.metlife.com/investments/en-us/insights/outlooks-and-market-updates/x/"
BODY = [
    "The results of the 2026 U.S. Congressional midterm elections are in. " * 8,
    "Even with Democrats controlling both chambers, divided government complicates governing. " * 7,
    "Second-term presidents have often turned their focus to legacy. " * 12,
    "Were the President to pursue a Social Security deal, a package could develop. " * 8,
]
SEARCH_WIDGET = (
    "<div class='navigation-search__wrapper page d-none'><div class='navigation-search__row'>"
    "<div class='error-body body-b2 richtext'><p>Sorry, we couldn't find any results matching</p>"
    "<p>Please:</p></div></div></div>")


def _new_template(paragraphs, extra=""):
    ps = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f"<html><body>{SEARCH_WIDGET}<main>{SEARCH_WIDGET}<div class='container'><div class='row'>"
            f"<div class='col-12 responsive-columns'><div class='richtext body-b1'>{ps}</div>"
            f"</div></div></div>{extra}</main></body></html>")


def _run(tmp_path, monkeypatch, page, final_url=URL):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    asked = []

    def fake_get(url, **k):
        asked.append(url)
        if ".pdf" in url:  # the PDF route fails: only the page itself could be stored
            return MagicMock(status_code=404, headers={"Content-Type": "text/html"}, content=b"")
        r = MagicMock(status_code=200, text=page, url=final_url)
        r.raise_for_status = lambda: None
        return r

    monkeypatch.setattr(fc.requests, "get", fake_get)
    fc.drain_extraction_paths()
    out = fc._fetch_content_metlife_im({"id": "ml-new", "title": "Midterm Results", "url": URL})
    return out, asked


def test_new_template_body_is_stored(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, _new_template(BODY))
    assert out is not None and out[1] == "ok"
    text = out[0].read_text()
    assert text.startswith("The results of the 2026")
    assert "Social Security deal" in text
    assert "Sorry, we couldn't find" not in text


def test_new_template_is_not_recorded_as_a_selector_miss(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _new_template(BODY))
    assert fc.drain_extraction_paths() == ["primary"]


def test_a_pdf_led_page_does_not_store_its_lead(tmp_path, monkeypatch):
    """Lead (579 chars measured) + Download PDF whose fetch fails: store nothing, retry."""
    button = ("<div><a class='btn' href='/content/dam/metlifecom/us/investments/pdf/MIM-x.pdf'>"
              "Download PDF</a></div>")
    out, asked = _run(tmp_path, monkeypatch, _new_template(BODY, extra=button))
    assert out is None
    assert any(".pdf" in u for u in asked)
    assert not list(tmp_path.iterdir())


def test_a_short_body_without_a_pdf_is_not_stored(tmp_path, monkeypatch):
    """A teaser-length body (the leads measure 267-579) is not an article."""
    out, _ = _run(tmp_path, monkeypatch, _new_template(["A short lead of a few hundred chars. " * 12]))
    assert out is None


def test_an_empty_page_is_still_a_selector_miss(tmp_path, monkeypatch):
    """Asia Pacific IG Bonds Q3 Recap: no read-more, no richtext body, no PDF."""
    out, _ = _run(tmp_path, monkeypatch, "<html><body><main><h1>Recap</h1></main></body></html>")
    assert out is None
    assert fc.drain_extraction_paths() == ["miss"]


def test_a_url_redirected_to_the_home_page_is_not_stored(tmp_path, monkeypatch):
    """Dead pinebridge.com URLs 30x to www.metlife.com/investments/en-us/, whose
    card blurbs passed 1,000 chars (1,024 measured 2026-11-07, Asia Pacific IG
    Bonds Q1 row). The fix requeues metlife's permafails, so without this guard
    the next run would store the home page as those articles' body."""
    home = "https://www.metlife.com/investments/en-us/"
    out, _ = _run(tmp_path, monkeypatch, _new_template(BODY), final_url=home)
    assert out is None
    assert not list(tmp_path.iterdir())


def test_a_trailing_slash_difference_is_not_a_redirect(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, _new_template(BODY), final_url=URL.rstrip("/"))
    assert out is not None
