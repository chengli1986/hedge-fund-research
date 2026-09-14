"""ARK Invest: video page bodies and escaped titles (2026-09-14).

From this server Cloudflare blocks ARK's articles, white papers and "Stock
Stories" videos (28 of 38 stored URLs answer "Just a moment..." / "Enable
JavaScript and cookies", with requests and with headless Chromium); that is
not worked around here. The ten "In The Know" video pages do load, and carry
a real description -- Cathie Wood's argument and figures -- in
div.single__content .wysiwyg p. None of the fetcher's selectors matched it,
so those pages fell to the metadata fallback and were stored as a bare title.

Titles came through the RSS feed still HTML-escaped: "ARK&apos;s Stock
Stories", "Why This &quot;Scary&quot; Jobs Report".
"""
from unittest.mock import MagicMock

import fetch_articles as fa
import fetch_content as fc

VIDEO = """<html><body><main><div class="single__content post video"><div class="has-zoom">
  <div class="wysiwyg">
    <p>“We have to go back to the Industrial Revolution to understand what’s going on today.” Global real
       gross domestic product growth has averaged 3% for 125 years. Cathie Wood thinks it at least doubles.</p>
    <p>In this month’s “In The Know,” Cathie connects the Industrial Revolution to today’s technology revolution.</p>
    <p>For more updates, follow us on X , LinkedIn , Facebook , Instagram .</p>
  </div></div></div>
  <div class="author-bio"><p>Cathie founded ARK to focus solely on investing in disruptive innovation.</p></div>
</main></body></html>"""


def _fetch(tmp_path, monkeypatch, html, status=200):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    resp = MagicMock(status_code=status, text=html)
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
    fc.drain_extraction_paths()
    out = fc._fetch_content_ark({"id": "ark-1", "url": "https://www.ark-invest.com/videos/market-commentary/x",
                                 "title": "Cathie Wood: The Market Is Pricing The Wrong Future"})
    return out, fc.drain_extraction_paths()


def test_a_video_page_description_is_the_body(tmp_path, monkeypatch):
    out, paths = _fetch(tmp_path, monkeypatch, VIDEO)
    assert out[1] == "ok" and paths == ["primary"]
    text = out[0].read_text()
    assert "averaged 3% for 125 years" in text and "connects the Industrial Revolution" in text


def test_the_social_follow_line_and_author_bio_are_not_body(tmp_path, monkeypatch):
    out, _ = _fetch(tmp_path, monkeypatch, VIDEO)
    text = out[0].read_text()
    assert "follow us on X" not in text and "Cathie founded ARK" not in text


def test_a_cloudflare_challenge_served_with_200_is_not_body(tmp_path, monkeypatch):
    """Some ARK pages answer 200 with the challenge ("Enable JavaScript and
    cookies to continue"); that must go to the metadata fallback, not be
    stored as the article."""
    challenge = "<html><body><p>Enable JavaScript and cookies to continue</p></body></html>"
    out, _ = _fetch(tmp_path, monkeypatch, challenge)
    assert out[1] == "metadata_only"


def test_rss_titles_are_unescaped(monkeypatch):
    feed = """<?xml version="1.0"?><rss><channel>
      <item><title>Tesla (TSLA): ARK&amp;apos;s Stock Stories</title>
        <link>https://www.ark-invest.com/videos/analyst-research/stock-stories-tsla</link>
        <pubDate>Thu, 13 Aug 2026 12:00:00 +0000</pubDate><category>Analyst Research</category></item>
      <item><title>Why This &amp;quot;Scary&amp;quot; Jobs Report Might Be Good News</title>
        <link>https://www.ark-invest.com/videos/market-commentary/august-2026-in-the-know</link>
        <pubDate>Sat, 08 Aug 2026 12:00:00 +0000</pubDate><category>Market Commentary</category></item>
    </channel></rss>"""
    resp = MagicMock(status_code=200, text=feed); resp.raise_for_status = lambda: None
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: resp)
    titles = [a["title"] for a in fa.fetch_ark_invest({"url": "https://www.ark-invest.com/feed", "max_articles": 10})]
    assert "Tesla (TSLA): ARK's Stock Stories" in titles
    assert 'Why This "Scary" Jobs Report Might Be Good News' in titles
