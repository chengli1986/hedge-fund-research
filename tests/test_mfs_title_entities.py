"""MFS Solr titles carry HTML entities the fetcher stored verbatim.

Measured 2026-09-26: doc "Who Owns the Outcome?&nbsp;AI and the New Economics
of Software" was stored with the six characters "&nbsp;" in the title, because
the fetcher only ran titles through BeautifulSoup when they contained "<".
publish.py html-escapes titles, so the reader saw "&nbsp;" on the page.
Found by the A/B against the api_json template.
"""
from unittest.mock import patch

import fetch_articles

SOURCE = {
    "id": "mfs-investment-management",
    "url": "https://www.mfs.com/en-us/investment-professional/insights/fixed-income.html",
    "expected_hostname": "www.mfs.com",
    "max_articles": 10,
}
URL = "https://www.mfs.com/en-us/investment-professional/insights/x.html"


def _docs(title):
    return {"response": {"docs": [{"title": title, "url": URL,
                                   "publisheddate": "September 9, 2026"}]}}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _title(raw):
    with patch("fetch_articles.requests.get", return_value=_Resp(_docs(raw))):
        return fetch_articles.fetch_mfs_investment_management(SOURCE)[0]["title"]


class TestMfsTitles:
    def test_an_entity_is_decoded_not_stored_verbatim(self):
        assert _title("Who Owns the Outcome?&nbsp;AI") == "Who Owns the Outcome? AI"

    def test_rich_text_markup_is_still_stripped(self):
        assert _title("<p>Market Pulse</p>") == "Market Pulse"

    def test_an_ampersand_entity_survives_as_one_character(self):
        assert _title("Rates &amp; Credit") == "Rates & Credit"

    def test_a_plain_title_is_unchanged(self):
        assert _title("Market Pulse") == "Market Pulse"
