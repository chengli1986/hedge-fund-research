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


# ── the article is a lead heading plus Key Takeaways, not <p> ──────────────
#
# "The Financing Gap in Sports: Unlocking a $2.5 Trillion Opportunity"
# (2026-08-11) was stored as 4,234 chars of disclaimer and declined as
# disclaimer_only. Measured on the live page 2026-09-16: .cmp-text holds three
# blocks -- a 230-char lead <h2>, a 1,450-char "Key Takeaways" <h3>+<ul>, and
# the 4,176-char standard disclaimer whose <p> tags were the only ones on the
# page. So ".cmp-text p" found the disclaimer and nothing else.
APOLLO_DISCLAIMER = ("The information herein is provided for educational purposes only and should not be "
                     "construed as financial or investment advice, nor should any information in this "
                     "document be relied on when making an investment decision. ") * 6
SPORTS_PAGE = (
    "<html><body>"
    "<div class='cmp-text'><h2>Sports are no longer just culture or entertainment; they are a scalable, "
    "investable asset class. Financing this business is becoming one of today's most compelling "
    "opportunities.</h2></div>"
    "<div class='cmp-text'><h3>Key Takeaways</h3><ul>"
    "<li>A $2.5 Trillion Global Industry: sports have expanded far beyond tickets and local sponsorships "
    "into a diversified global ecosystem spanning media rights, streaming, betting, sponsorship, "
    "hospitality and real estate around the venues themselves, with revenues growing through cycles.</li>"
    "<li>The financing gap: leagues and clubs need patient capital that traditional lenders are not "
    "structured to provide, because the collateral is contracted media revenue rather than hard assets, "
    "and bank balance sheets have retreated from long-dated, structured exposures since 2023.</li>"
    "<li>Where Apollo plays: a long-term partner across the global sports and live events ecosystem, "
    "providing patient capital to teams, leagues and the infrastructure that surrounds them, from "
    "stadium financing to media-rights securitisation and growth equity in adjacent businesses.</li>"
    "<li>Why now: institutional ownership rules have loosened across major leagues, media rights keep "
    "repricing upward, and the financing need is larger than the capital currently available to meet "
    "it, which is what makes the return profile compelling for long-duration investors today.</li>"
    "<li>What to watch: club valuations have outrun cash flows in some leagues, so underwriting has to "
    "rest on contracted revenue and downside protection rather than on multiple expansion, and the "
    "next repricing of media rights is the single biggest swing factor for the asset class.</li>"
    "<li>How it is financed: structures range from senior secured facilities against media receivables "
    "to hybrid capital that sits between debt and equity, letting owners fund stadiums and roster "
    "investment without giving up control of the franchise itself.</li>"
    "</ul></div>"
    f"<div class='cmp-text'><p>{APOLLO_DISCLAIMER}</p></div>"
    "</body></html>")


def test_an_article_written_as_headings_and_lists_is_read(tmp_path, monkeypatch):
    out, asked = _run(tmp_path, monkeypatch, SPORTS_PAGE, PDF,
                      title="The Financing Gap in Sports: Unlocking a $2.5 Trillion Opportunity")
    assert out is not None and out[1] == "ok"
    body = out[0].read_text()
    assert "scalable, investable asset class" in body and "A $2.5 Trillion Global Industry" in body
    assert not any(u.endswith(".pdf") for u in asked), "the page had an article; no PDF was needed"


def test_the_standard_disclaimer_is_not_the_body(tmp_path, monkeypatch):
    out, _ = _run(tmp_path, monkeypatch, SPORTS_PAGE, PDF,
                  title="The Financing Gap in Sports: Unlocking a $2.5 Trillion Opportunity")
    assert "provided for educational purposes only" not in out[0].read_text()


def test_a_page_that_is_only_the_disclaimer_is_still_skipped(tmp_path, monkeypatch):
    """Without the article blocks there is nothing to summarise, and inventing
    a body out of the disclaimer is what the decline caught in the first place."""
    page = f"<html><body><div class='cmp-text'><p>{APOLLO_DISCLAIMER}</p></div></body></html>"
    out, _ = _run(tmp_path, monkeypatch, page, PDF, title="Some Apollo Piece")
    assert out is None
