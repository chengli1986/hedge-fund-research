"""ARK's hand-written fetcher and its listing template subscribe the same categories.

0b14ee6 (2026-10-30) added six categories to the template in sources.json only.
The template is what production runs, but the weekly A/B gate
(scripts/compare_fetchers.py) still compares it against fetch_ark_invest, which
kept the old four -- so the two returned different newest-10 sets and the gate
failed on 2026-11-02. Both lists now match and this test keeps them matched.
"""
import json
from pathlib import Path

import pytest

import fetch_articles as fa

ROOT = Path(__file__).resolve().parent.parent


def _template_categories() -> list[str]:
    sources = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))["sources"]
    ark = next(s for s in sources if s["id"] == "ark-invest")
    return ark["listing_template"]["categories"]


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _feed(category: str) -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<item><title>Post</title><link>https://www.ark-invest.com/x/post</link>"
        "<pubDate>Mon, 26 Oct 2026 12:00:00 +0000</pubDate>"
        f"<category>{category.title()}</category><description>d</description></item>"
        "</channel></rss>"
    )


@pytest.mark.parametrize("category", _template_categories())
def test_hand_written_fetcher_keeps_every_template_category(monkeypatch, category):
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: _Resp(_feed(category)))
    rows = fa.fetch_ark_invest({"id": "ark-invest", "name": "ARK Invest",
                                "url": "https://www.ark-invest.com/feed"})
    assert len(rows) == 1, f"fetch_ark_invest drops category {category!r} the template keeps"


def test_hand_written_fetcher_drops_a_category_neither_subscribes(monkeypatch):
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: _Resp(_feed("Press Releases")))
    rows = fa.fetch_ark_invest({"id": "ark-invest", "name": "ARK Invest",
                                "url": "https://www.ark-invest.com/feed"})
    assert rows == []
