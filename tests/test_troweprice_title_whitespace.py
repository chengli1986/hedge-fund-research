"""T. Rowe Price titles carry non-breaking spaces.

Measured 2026-09-25: the live listing renders "The Long View:\xa0Wael Sawan"
and get_text(strip=True) stores the U+00A0 verbatim, while every other
whitespace run is collapsed. The A/B against the listing template disagreed on
exactly those two rows and nothing else. _title_key NFKC-normalises, so
dedup was never affected -- this is about what gets stored and displayed.
"""
from unittest.mock import patch

import fetch_articles

SOURCE = {
    "id": "troweprice",
    "url": "https://www.troweprice.com/en/us/insights",
    "expected_hostname": "www.troweprice.com",
    "max_articles": 10,
}


def _page(title_html: str) -> str:
    return (
        '<div class="b-grid-item--12-col">'
        f'<h2 class="beacon-article-tile__title"><a href="/en/us/insights/x">{title_html}</a></h2>'
        '<span class="beacon-article-tile__eyebrow">September 3, 2026 · Markets</span>'
        '</div>'
    )


class TestTitleWhitespace:
    def _titles(self, title_html):
        with patch("fetch_articles._get_playwright_page", return_value=_page(title_html)):
            return [a["title"] for a in fetch_articles.fetch_troweprice(SOURCE)]

    def test_a_non_breaking_space_is_normalised(self):
        assert self._titles("The Long View: Wael Sawan") == ["The Long View: Wael Sawan"]

    def test_inline_tags_do_not_fuse_words(self):
        assert self._titles("A New Blueprint for<span>Infrastructure</span>") == [
            "A New Blueprint for Infrastructure"]

    def test_the_date_still_comes_out_of_the_eyebrow(self):
        with patch("fetch_articles._get_playwright_page", return_value=_page("T")):
            got = fetch_articles.fetch_troweprice(SOURCE)
        assert got[0]["date"] == "2026-09-03" and got[0]["category"] == "Markets"
