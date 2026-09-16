"""G&R: a hardcoded host the guard could not see, and a modification time
stored as the publication date (audit finding B6, 2026-09-16).

Two defects in one fetcher:

1. `_validate_hostname(url, "blog.gorozen.com")` ignored
   source["expected_hostname"] (config says "gorozen.com"). The fleet-wide
   guard in tests/test_no_hardcoded_hosts.py exists to stop exactly this, but
   its detector matched `https?://`, so a BARE hostname literal was invisible
   to it -- a guard that believed it was covering the case. Move the blog and
   this fetcher returns 0 articles with no error, which is the Research
   Affiliates outage reproduced under a guard.

2. `"date": date_raw` stored the sitemap <lastmod> -- a MODIFICATION time --
   as the publication date, unparsed. Measured on the live pages: "Why Haven't
   the Tanks Run Dry" carries datePublished 2026-08-07 and dateModified
   2026-08-27, and the sitemap serves the latter. So a re-render moved the
   article 20 days forward, which after the 2026-09-16 reused-URL change would
   mint a duplicate issue row every time the site re-renders. The article page
   is already fetched for its title, and it carries JSON-LD datePublished.
"""
import json
from unittest.mock import MagicMock

import pytest

import fetch_articles as fa

SRC = {"id": "goehring-rozencwajg", "name": "G&R", "short_name": "G&R", "method": "sitemap",
       "url": "https://blog.gorozen.com/blog", "expected_hostname": "gorozen.com",
       "max_articles": 2}

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://blog.gorozen.com/blog/tanks-run-dry</loc><lastmod>2026-08-27</lastmod></url>
</urlset>"""

LD = ('{"@context":"http://schema.org","@type":"BlogPosting",'
      '"headline":"Why Haven\'t the Tanks Run Dry?",'
      '"datePublished":"2026-08-07T15:51:45.000Z",'
      '"dateModified":"2026-08-27T00:32:52.790Z"}')

PAGE = ('<html><head><meta property="og:title" content="Why Haven\'t the Tanks Run Dry? '
        '| Goehring &amp; Rozencwajg">'
        f'<script type="application/ld+json">{LD}</script></head><body>x</body></html>')


def _run(monkeypatch, sitemap=SITEMAP, page=PAGE, source=None):
    def fake_get(url, **kw):
        body = sitemap if url.endswith("sitemap.xml") else page
        r = MagicMock(status_code=200, text=body)
        r.raise_for_status = lambda: None
        return r
    monkeypatch.setattr(fa.requests, "get", fake_get)
    return fa.fetch_goehring_rozencwajg(dict(source or SRC))


def test_the_publication_date_is_used_not_the_modification_time(monkeypatch):
    got = _run(monkeypatch)
    assert len(got) == 1
    assert got[0]["date"] == "2026-08-07", "stored the sitemap lastmod (a re-render) as the date"


def test_the_raw_date_records_where_it_came_from(monkeypatch):
    got = _run(monkeypatch)
    assert "2026-08-07" in (got[0].get("date_raw") or "")


def test_a_page_without_structured_data_falls_back_to_the_sitemap(monkeypatch):
    plain = '<html><head><meta property="og:title" content="Tanks"></head><body>x</body></html>'
    got = _run(monkeypatch, page=plain)
    assert len(got) == 1 and got[0]["date"] == "2026-08-27"


def test_the_host_comes_from_the_config_not_a_literal(monkeypatch):
    """The config host is the parent domain; the articles sit on a subdomain."""
    got = _run(monkeypatch)
    assert len(got) == 1


def test_a_moved_blog_follows_the_config(monkeypatch):
    """A domain move is meant to be a config edit, not an outage."""
    moved_sitemap = SITEMAP.replace("blog.gorozen.com", "research.gorozen.io")
    src = dict(SRC, url="https://research.gorozen.io/blog", expected_hostname="gorozen.io")
    got = _run(monkeypatch, sitemap=moved_sitemap, source=src)
    assert len(got) == 1, "the fetcher still validates against the old hardcoded host"


def test_an_article_off_the_declared_host_is_still_rejected(monkeypatch):
    evil = SITEMAP.replace("blog.gorozen.com", "blog.gorozen.com.evil.test")
    assert _run(monkeypatch, sitemap=evil) == []


class TestTheGuardSeesBareHostnames:
    def test_a_bare_hostname_literal_is_detected(self):
        """The guard matched https?:// only, so "blog.gorozen.com" slipped past
        it for as long as the literal existed."""
        import importlib
        guard = importlib.import_module("tests.test_no_hardcoded_hosts") \
            if False else __import__("test_no_hardcoded_hosts")

        def sample(source):
            return _validate_hostname("https://x/y", "blog.gorozen.com")   # noqa: F821

        found = guard._runtime_url_literals(sample)
        assert any("gorozen.com" in f for f in found), (
            "a bare hostname passed to _validate_hostname is invisible to the guard")
