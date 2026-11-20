"""Robeco insights redesign (between 2026-11-17 19:47 and 2026-11-18 19:48 UTC).

The old cards `div.clickable-wrapper.card` vanished, so three nights returned 0
articles with no error (the wait-selector timeout is swallowed). New cards are
`div.card[data-testid='grid-list-item']` with `h4.card-title a` and
`time[data-testid='app-date']` (DD-MM-YYYY). The page is NOT in date order: a
highlight card (an older piece) comes first, a curated list mixes in months-old
items, and a second list repeats some of the newest -- so taking the first N cards
in page order can miss the newest article.
"""
import fetch_articles as fa

SOURCE = {
    "id": "robeco",
    "url": "https://www.robeco.com/en-int/insights",
    "expected_hostname": "robeco.com",
    "max_articles": 3,
}


def _card(date: str, slug: str, title: str) -> str:
    return (
        "<div data-testid='grid-list-item' class='card'>"
        f"<h4 class='card-title'><a href='/en-int/insights/{slug}'>{title}</a></h4>"
        f"<time data-testid='app-date'>{date}</time>"
        "</div>"
    )


PAGE = (
    "<html><body>"
    "<div class='highlight-wrapper'>"
    + _card("28-10-2026", "2026/10/defensive-equities", "Defensive equities")
    + "</div><div class='list-item'>"
    + _card("20-11-2026", "2026/11/chips-are-down", "Why the chips are down")
    + _card("02-10-2026", "2026/09/new-geoeconomics", "New geoeconomics")
    + _card("16-11-2026", "2026/11/ai-capex", "AI capex boom")
    + _card("22-09-2026", "2026/09/podcast-great-rewiring", "Podcast: great rewiring")
    + _card("13-11-2026", "2026/11/consumer-stocks", "Consumer stocks")
    + "</div><div class='list-item'>"
    + _card("20-11-2026", "2026/11/chips-are-down", "Why the chips are down")
    + "</div></body></html>"
)


def _fetch(monkeypatch, html=PAGE):
    seen = {}

    def fake_page(url, **kw):
        seen.update(kw)
        return html

    monkeypatch.setattr(fa, "_get_playwright_page", fake_page)
    return fa.fetch_robeco(SOURCE), seen


def test_new_cards_are_read(monkeypatch):
    arts, _ = _fetch(monkeypatch)
    assert arts, "new card markup must yield articles"
    assert all(a["url"].startswith("https://www.robeco.com/en-int/insights/") for a in arts)
    assert all(a["title"] and a["date"] for a in arts)


def test_waits_for_the_new_card_selector(monkeypatch):
    _, kw = _fetch(monkeypatch)
    assert kw.get("wait_selector") == "div.card[data-testid='grid-list-item']"


def test_newest_articles_win_over_page_order(monkeypatch):
    arts, _ = _fetch(monkeypatch)
    assert [a["date"] for a in arts] == ["2026-11-20", "2026-11-16", "2026-11-13"]
    assert arts[0]["title"] == "Why the chips are down"


def test_repeated_cards_and_podcasts_are_dropped(monkeypatch):
    big = dict(SOURCE, max_articles=10)
    monkeypatch.setattr(fa, "_get_playwright_page", lambda *a, **k: PAGE)
    arts = fa.fetch_robeco(big)
    urls = [a["url"] for a in arts]
    assert len(urls) == len(set(urls))
    assert not any("podcast" in u for u in urls)
    assert len(arts) == 5


def test_dates_are_day_first(monkeypatch):
    arts, _ = _fetch(monkeypatch)
    one = [a for a in arts if a["title"] == "Consumer stocks"][0]
    assert one["date"] == "2026-11-13" and one["date_raw"] == "13-11-2026"
