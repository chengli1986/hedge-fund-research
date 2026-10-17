"""Partners Group PDF cards (2026-10-17).

The fetcher kept only /news-and-views/perspective/<slug> cards and dropped the
PDF cards (/pdf-viewer.aspx?...&src=<file>.pdf) on the belief that a PDF had
no extractable body. About half of the listing is PDF (quarterly Chartbooks,
liquid credit commentary, white papers): the 2026-10-06 Chartbook was never
fetched and the health probe warned "most recent article 93d old". Both PDFs
checked that day extract (25k / 16k chars) and summarise; fetch_content's
generic PDF path takes any URL whose path ends in .pdf.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fetch_articles  # noqa: E402
import fetch_content  # noqa: E402

SOURCE = {"id": "partners-group", "url": "https://www.partnersgroup.com/en/news-and-views/perspective",
          "expected_hostname": "partnersgroup.com", "max_articles": 10}


def _card(date, title, href):
    return (f'<div class="per-card"><span class="tag-lg-text">{date} Macro</span>'
            f'<h2 class="per-card__title">{title}</h2>'
            f'<div class="per-card__cta-wrapper"><a href="{href}">x</a></div></div>')


PAGE = "".join([
    _card("06.10.2026", "Private Markets Chartbook –Higher-for-Longer",
          "/pdf-viewer.aspx?gid=443092680&src=/~/media/Files/P/Partnersgroup/Universal/"
          "perspectives-document/private-markets-chartbook-q4-2026-higher-for-longer.pdf"),
    _card("16.07.2026", "2026 Mid-Year Outlook",
          "https://www.partnersgroup.com/en/news-and-views/perspective/midyear-outlook-2026"),
    _card("24.04.2025", "Quarterly Liquid Credit Market Commentary: Q1 2025",
          "/pdf-viewer.aspx?gid=443092680&src=/~/media/Files/P/Partnersgroup/Universal/"
          "perspectives-document/20250424_Q1 2025_Partners Group_Issue 23.pdf"),
    _card("15.10.2025", "Velvet CARE’s transformation journey", "/en/news-and-views/videos"),
    _card("01.01.2026", "Elsewhere", "/pdf-viewer.aspx?src=https://evil.example.com/x.pdf"),
])


def _fetch(monkeypatch):
    class Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass
    monkeypatch.setattr(fetch_articles.requests, "get",
                        lambda url, **kw: Resp(PAGE if "page=" not in url else ""))
    return fetch_articles.fetch_partners_group(SOURCE)


def test_pdf_cards_are_kept_as_direct_pdf_urls(monkeypatch):
    by_title = {a["title"]: a for a in _fetch(monkeypatch)}
    chart = by_title["Private Markets Chartbook –Higher-for-Longer"]
    assert chart["url"] == ("https://www.partnersgroup.com/~/media/Files/P/Partnersgroup/Universal/"
                            "perspectives-document/private-markets-chartbook-q4-2026-higher-for-longer.pdf")
    assert chart["date"] == "2026-10-06"
    # fetch_content routes it to the generic PDF download
    assert fetch_content.content_fetcher_for({"source_id": "partners-group", "url": chart["url"]}) \
        is fetch_content._fetch_content_pdf_url


def test_spaces_in_a_pdf_path_are_encoded(monkeypatch):
    urls = [a["url"] for a in _fetch(monkeypatch)]
    assert ("https://www.partnersgroup.com/~/media/Files/P/Partnersgroup/Universal/perspectives-document/"
            "20250424_Q1%202025_Partners%20Group_Issue%2023.pdf") in urls


def test_articles_still_kept_and_videos_and_offsite_pdfs_still_dropped(monkeypatch):
    titles = [a["title"] for a in _fetch(monkeypatch)]
    assert "2026 Mid-Year Outlook" in titles
    assert "Velvet CARE’s transformation journey" not in titles
    assert "Elsewhere" not in titles
    assert titles[0] == "Private Markets Chartbook –Higher-for-Longer"     # newest first


def test_the_health_probe_reads_a_pdf_article_as_stage_2_does(monkeypatch, tmp_path):
    """The probe took CONTENT_FETCHERS[id] for every article; stage 2 sends a
    .pdf URL to the PDF reader. With the Chartbook listed first, the probe ran
    the HTML fetcher on a PDF."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gfh_pg", ROOT / "scripts" / "gmia-fetcher-health.py")
    gfh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gfh)
    pdf = "https://www.partnersgroup.com/~/media/x/chartbook.pdf"
    src = {"id": "pg-probe", "frequency": "monthly"}
    monkeypatch.setattr(fetch_articles, "listing_fetcher",
                        lambda s: lambda s2: [{"title": "C", "url": pdf, "date": "2026-10-06"}])
    monkeypatch.setitem(fetch_content.CONTENT_FETCHERS, "pg-probe", lambda a: None)
    body = tmp_path / "b.txt"
    body.write_text("x" * 500)
    seen = []
    monkeypatch.setattr(fetch_content, "_fetch_content_pdf_url",
                        lambda a: seen.append(a["url"]) or (body, "ok"))
    result = gfh.probe_source(src)
    assert seen == [pdf]
    assert result["content_chars"] == 500
