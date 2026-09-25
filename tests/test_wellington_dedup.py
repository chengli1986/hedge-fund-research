"""Wellington's listing repeats the same card several times.

Measured 2026-09-25 on the live page: the hand-written fetcher returned 10
rows holding only 6 distinct URLs, so 4 of the 10 max_articles slots were
spent on copies and 4 real articles never got ingested.  Found by the A/B
run against the declarative template, which dedups by URL.
"""
import pytest

import fetch_articles


def _card(slug: str, title: str) -> str:
    return (
        '<section class="insight article">'
        f'<a class="insight__title" href="/en/insights/{slug}">{title}</a>'
        f'<a class="insight__link" href="/en/insights/{slug}"></a>'
        '<date datetime="2026-09-18">18 September 2026</date>'
        '</section>'
    )


SOURCE = {
    "id": "wellington",
    "url": "https://www.wellington.com/en/insights",
    "expected_hostname": "www.wellington.com",
    "max_articles": 3,
}


class TestWellingtonDedup:
    def test_repeated_cards_do_not_eat_the_article_budget(self, monkeypatch):
        html = "".join([
            _card("a", "Alpha"), _card("b", "Beta"), _card("a", "Alpha"),
            _card("c", "Gamma"), _card("b", "Beta"),
        ])
        monkeypatch.setattr(fetch_articles, "_get_playwright_page",
                            lambda *a, **k: html)
        got = fetch_articles.fetch_wellington(SOURCE)
        assert [a["title"] for a in got] == ["Alpha", "Beta", "Gamma"]

    def test_distinct_articles_are_all_kept(self, monkeypatch):
        html = _card("a", "Alpha") + _card("b", "Beta")
        monkeypatch.setattr(fetch_articles, "_get_playwright_page",
                            lambda *a, **k: html)
        got = fetch_articles.fetch_wellington(SOURCE)
        assert len(got) == 2
