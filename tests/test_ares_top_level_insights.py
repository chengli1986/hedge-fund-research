"""Ares: new research stopped landing under /perspectives/ (2026-10-07).

The health check had warned "stale: most recent article 42d old" for 12 runs.
True of /perspectives/ -- its newest sitemap lastmod is 2026-08-27 -- but not
of Ares: "Beyond the Game" (tag "Private Market Insights", 2026-08-31) and
"The 50/50 Portfolio" (tag "Education", 2026-07-08) were published at the top
level, /us/news-and-insights/<slug>, which the fetcher never looked at.

That top level is mixed, so it is filtered on the article page itself:
- tag "Media" is a video/interview stub (a few hundred chars, no body);
- re-edited old pieces keep their original date ("Using Private Markets",
  Jul 2023, sitemap lastmod 2026-09-10), so a publication-date floor keeps
  the back catalogue out;
- hub pages (In the Gaps) carry no share-print-date. Their PDFs are marked
  "confidential, and must not be forwarded" and are not collected.
"""
from unittest.mock import MagicMock

import fetch_articles as fa

BASE = "https://www.ares.com/us/news-and-insights"
SRC = {"id": "ares-management", "name": "Ares Management", "short_name": "Ares",
       "method": "ssr", "url": f"{BASE}/perspectives", "expected_hostname": "ares.com",
       "max_articles": 10, "fetch_shape": "sitemap_plus_page"}


def _page(title, date, tag):
    date_html = f'<span class="share-print-date">{date}</span>' if date else ""
    return ('<html><body><div class="campaign-hero-content"><div class="share-print-meta">'
            f'<div class="share-print-tag">{tag}</div>'
            f'<div class="share-print-data">{date_html}</div></div>'
            f'<h1>{title}</h1></div></body></html>')


PAGES = {
    f"{BASE}/perspectives/what-is-infrastructure":
        _page("What Is Infrastructure?", "Aug 26, 2026", "Perspectives"),
    f"{BASE}/beyond-the-game":
        _page("Beyond the Game", "Aug 31, 2026", "Private Market Insights"),
    f"{BASE}/5050-portfolio":
        _page("The 50/50 Portfolio", "Jul 08, 2026", "Education"),
    f"{BASE}/a-box-seat":
        _page("A box seat", "Sep 21, 2026", "Media"),
    f"{BASE}/using-private-markets":
        _page("Using Private Markets", "Jul 11, 2023", "Education"),
    f"{BASE}/in-the-gaps":
        _page("In the Gaps", None, ""),
    f"{BASE}/press-releases/ares-reports-q3":
        _page("Ares reports Q3", "Oct 01, 2026", "Press Release"),
}

LASTMOD = {
    f"{BASE}/perspectives/what-is-infrastructure": "2026-08-26T14:00:59.494Z",
    f"{BASE}/beyond-the-game": "2026-09-02T05:19:56.584Z",
    f"{BASE}/5050-portfolio": "2026-08-05T10:00:00.000Z",
    f"{BASE}/a-box-seat": "2026-10-01T06:08:03.643Z",
    f"{BASE}/using-private-markets": "2026-09-10T18:45:47.204Z",
    f"{BASE}/in-the-gaps": "2026-09-28T15:43:50.137Z",
    f"{BASE}/press-releases/ares-reports-q3": "2026-10-02T00:00:00.000Z",
}


def _sitemap(lastmod=LASTMOD):
    rows = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in lastmod.items())
    return f'<?xml version="1.0"?><urlset>{rows}</urlset>'


def _run(monkeypatch, sitemap=None, pages=PAGES):
    sitemap = sitemap or _sitemap()
    requested = []

    def fake_get(url, **kw):
        requested.append(url)
        if url.endswith("sitemap.xml"):
            body, code = sitemap, 200
        else:
            body, code = pages.get(url, ""), 200 if url in pages else 404
        r = MagicMock(status_code=code, text=body)
        r.raise_for_status = lambda: None
        return r
    monkeypatch.setattr(fa.requests, "get", fake_get)
    return fa.fetch_ares_management(dict(SRC)), requested


def _urls(got):
    return {a["url"] for a in got}


def test_top_level_insights_are_collected(monkeypatch):
    got, _ = _run(monkeypatch)
    assert f"{BASE}/beyond-the-game" in _urls(got)
    assert f"{BASE}/5050-portfolio" in _urls(got)


def test_perspectives_are_still_collected(monkeypatch):
    got, _ = _run(monkeypatch)
    assert f"{BASE}/perspectives/what-is-infrastructure" in _urls(got)


def test_media_stubs_are_skipped(monkeypatch):
    got, _ = _run(monkeypatch)
    assert f"{BASE}/a-box-seat" not in _urls(got)


def test_re_edited_back_catalogue_is_skipped(monkeypatch):
    """lastmod is recent, the page's own date is 2023."""
    got, _ = _run(monkeypatch)
    assert f"{BASE}/using-private-markets" not in _urls(got)


def test_a_top_level_page_without_a_date_is_skipped(monkeypatch):
    """Hubs have no share-print-date; perspectives fall back to lastmod, these must not."""
    got, _ = _run(monkeypatch)
    assert f"{BASE}/in-the-gaps" not in _urls(got)


def test_nested_sections_other_than_perspectives_are_not_read(monkeypatch):
    got, requested = _run(monkeypatch)
    assert f"{BASE}/press-releases/ares-reports-q3" not in _urls(got)
    assert f"{BASE}/press-releases/ares-reports-q3" not in requested


def test_the_newest_articles_win_the_cap(monkeypatch):
    """Ten August perspectives must not crowd out a newer top-level article."""
    lastmod = dict(LASTMOD)
    pages = dict(PAGES)
    for i in range(12):
        u = f"{BASE}/perspectives/p{i}"
        lastmod[u] = f"2026-08-{10 + i:02d}T00:00:00.000Z"
        pages[u] = _page(f"Perspective {i}", f"Aug {10 + i:02d}, 2026", "Perspectives")
    got, _ = _run(monkeypatch, sitemap=_sitemap(lastmod), pages=pages)
    assert len(got) == SRC["max_articles"]
    assert f"{BASE}/beyond-the-game" in _urls(got)
    dates = [a["date"] for a in got]
    assert dates == sorted(dates, reverse=True)


def test_dates_are_parsed_from_the_page(monkeypatch):
    got, _ = _run(monkeypatch)
    by_url = {a["url"]: a for a in got}
    assert by_url[f"{BASE}/beyond-the-game"]["date"] == "2026-08-31"
