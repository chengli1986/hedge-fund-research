"""Robeco insights redesign (between 2026-11-17 19:47 and 2026-11-18 19:48 UTC).

The old cards `div.clickable-wrapper.card` vanished, so three nights returned 0
articles with no error (the wait-selector timeout is swallowed). New cards are
`div.card[data-testid='grid-list-item']`; the markup below is trimmed from the
page captured on EC2 on 2026-11-21 (`div.meta` = date + " · " + type label,
`h4.card-title > a > span.title`). The page is NOT in date order: a highlight
card (an older piece) comes first, a curated list mixes in months-old items, and
a second list repeats some of the newest. Most podcasts now use an ordinary
/insights/YYYY/MM/<slug> path, so only the type label tells them apart.
"""
import fetch_articles as fa

SOURCE = {
    "id": "robeco",
    "url": "https://www.robeco.com/en-int/insights",
    "expected_hostname": "robeco.com",
    "max_articles": 3,
}


def _card(date: str, kind: str, slug: str, title: str, host: str = "") -> str:
    meta = (f"<time data-testid='app-date'>{date}</time><!--[--> · <!--]-->"
            if date else "")
    return (
        "<div class='card gradient-mint default card' data-testid='grid-list-item'>"
        "<div class='text'><div class='meta selectable'>"
        f"{meta}<!--[-->{kind}<!--]--></div>"
        "<h4 class='card-title'><a class='app-link link stretched' data-testid='app-link'"
        f" href='{host}/en-int/insights/{slug}'><span class='title'>{title}</span></a></h4>"
        "<div aria-hidden='true' class='read-more'>Learn more</div></div></div>"
    )


PAGE = (
    "<html><body>"
    "<div class='highlight-wrapper'>"
    + _card("28-10-2026", "Insight", "2026/10/defensive-equities", "Defensive equities")
    + "</div><div class='list-item'>"
    + _card("20-11-2026", "Podcast", "2026/11/in-tune-with-the-markets-chips",
            "In tune with the markets – Why the chips are down")
    + _card("", "Insight", "2026/11/ai-capex", "AI capex boom")
    + _card("02-10-2026", "Insight", "2026/09/new-geoeconomics", "New geoeconomics")
    + _card("13-11-2026", "Insight", "2026/11/consumer-stocks", "Consumer stocks")
    + _card("22-09-2026", "Video", "2026/09/expected-returns-video", "Expected returns video")
    + _card("19-11-2026", "Insight", "2026/11/elsewhere", "Off-site copy",
            host="https://www.example.com")
    + "</div><div class='list-item'>"
    + _card("16-11-2026", "Insight", "2026/11/ai-capex", "AI capex boom")
    + _card("13-11-2026", "Insight", "2026/11/consumer-stocks", "Consumer stocks")
    + "</div></body></html>"
)


def _fetch(monkeypatch, source=SOURCE):
    seen = {}

    def fake_page(url, **kw):
        seen.update(kw)
        return PAGE

    monkeypatch.setattr(fa, "_get_playwright_page", fake_page)
    return fa.fetch_robeco(source), seen


def test_waits_for_the_new_card_selector(monkeypatch):
    _, kw = _fetch(monkeypatch)
    assert kw.get("wait_selector") == "div.card[data-testid='grid-list-item']"


def test_newest_text_articles_win_over_page_order(monkeypatch):
    arts, _ = _fetch(monkeypatch)
    assert [(a["date"], a["title"]) for a in arts] == [
        ("2026-11-16", "AI capex boom"),
        ("2026-11-13", "Consumer stocks"),
        ("2026-10-28", "Defensive equities"),
    ]
    assert arts[0]["url"] == "https://www.robeco.com/en-int/insights/2026/11/ai-capex"
    assert arts[1]["date_raw"] == "13-11-2026"


def test_podcast_and_video_cards_are_skipped_by_label(monkeypatch):
    arts, _ = _fetch(monkeypatch, dict(SOURCE, max_articles=10))
    titles = {a["title"] for a in arts}
    assert "In tune with the markets – Why the chips are down" not in titles
    assert "Expected returns video" not in titles


def test_off_site_links_are_dropped(monkeypatch):
    arts, _ = _fetch(monkeypatch, dict(SOURCE, max_articles=10))
    assert all(a["url"].startswith("https://www.robeco.com/") for a in arts)


def test_repeats_collapse_and_a_dated_copy_beats_an_undated_one(monkeypatch):
    arts, _ = _fetch(monkeypatch, dict(SOURCE, max_articles=10))
    urls = [a["url"] for a in arts]
    assert len(urls) == len(set(urls)) == 4
    capex = [a for a in arts if a["title"] == "AI capex boom"][0]
    assert capex["date"] == "2026-11-16"
