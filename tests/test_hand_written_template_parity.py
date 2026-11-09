"""Hand-written fetchers return what their listing template returns, on the same input.

The weekly A/B gate (scripts/compare_fetchers.py) failed on 2026-11-09 for two
sources whose template had been fixed and whose hand-written reference had not.
Production runs the template; the hand-written fetcher is the comparison and
the way back, so it is the side that is fixed (see the wellington/troweprice
precedent in compare_fetchers.py).

- ark-invest: "Cathie Wood: Did The Market Overreact To The Election?&nbsp;| In
  The Know" (2026-11-07). fetch_ark_invest kept the unescaped \\xa0 in the
  title; the template collapses whitespace.
- blue-owl-capital: some cards link /index%2Ephp/insights/<slug>. bc43520
  (2026-10-28) strips that front controller in the template only;
  fetch_blue_owl_capital kept its /insights/ path check and dropped the card
  (the 2026-09-23 Anaplan piece).

Each test runs both sides on one page, so the two cannot drift apart again
without one of these going red.
"""
import json
from pathlib import Path

import fetch_articles as fa
import listing_templates as lt

ROOT = Path(__file__).resolve().parent.parent


def _source(sid: str) -> dict:
    sources = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))["sources"]
    return next(s for s in sources if s["id"] == sid)


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _rows(rows):
    return [(r["title"], r["url"], r["date"]) for r in rows]


ARK_FEED = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<item><title>Cathie Wood: Did The Market Overreact To The Election?&amp;nbsp;| In The Know</title>"
    "<link>https://www.ark-invest.com/videos/market-commentary/november-2026-in-the-know-cathie-wood</link>"
    "<pubDate>Sat, 07 Nov 2026 12:00:00 +0000</pubDate>"
    "<category>Market Commentary</category><description>d</description></item>"
    "<item><title>Two  spaces  and\ttab</title><link>https://www.ark-invest.com/articles/x</link>"
    "<pubDate>Fri, 06 Nov 2026 12:00:00 +0000</pubDate>"
    "<category>Analyst Research</category><description>d</description></item>"
    "</channel></rss>"
)


def test_ark_titles_match_the_template(monkeypatch):
    source = _source("ark-invest")
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: _Resp(ARK_FEED))
    hand = fa.fetch_ark_invest(source)
    template = lt.parse_feed(ARK_FEED, source, source["listing_template"])
    assert hand and "\xa0" not in hand[0]["title"]
    assert _rows(hand) == _rows(template)


BLUE_OWL_PAGE = """
<a class="article-listing__card" href="/index%2Ephp/insights/blue-owl-executive-perspectives-anaplan">
  <time datetime="2026-09-23T12:00:00-04:00">September 23, 2026</time>
  <h3 class="article-listing__copy"><span>Blue Owl Executive Perspectives: Anaplan</span></h3></a>
<a class="article-listing__card" href="/insights/older-piece">
  <time datetime="2025-06-01T12:00:00-04:00">June 1, 2025</time>
  <h3 class="article-listing__copy"><span>Older piece</span></h3></a>
<a class="article-listing__card" href="https://docs.blueowl.com/viewer/x.pdf">
  <time datetime="2026-10-01T12:00:00-04:00">October 1, 2026</time>
  <h3 class="article-listing__copy"><span>PDF viewer</span></h3></a>
"""


def test_blue_owl_keeps_a_front_controller_card_like_the_template(monkeypatch):
    source = _source("blue-owl-capital")
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: _Resp(BLUE_OWL_PAGE))
    hand = fa.fetch_blue_owl_capital(source)
    template = lt.parse_cards(BLUE_OWL_PAGE, source, source["listing_template"])
    assert [r["url"] for r in hand] == [
        "https://www.blueowl.com/insights/blue-owl-executive-perspectives-anaplan",
        "https://www.blueowl.com/insights/older-piece",
    ]
    assert sorted(_rows(hand)) == sorted(_rows(template))
