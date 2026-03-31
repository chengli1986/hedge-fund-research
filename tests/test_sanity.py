import json
import pytest
from pathlib import Path

pytestmark = pytest.mark.live

class TestLiveSourceAccess:
    def test_man_group_live(self):
        from fetch_articles import fetch_man_group
        articles = fetch_man_group({"url": "https://www.man.com/insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_bridgewater_live(self):
        from fetch_articles import fetch_bridgewater
        articles = fetch_bridgewater({"url": "https://www.bridgewater.com/research-and-insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]

    def test_aqr_live(self):
        from fetch_articles import fetch_aqr
        articles = fetch_aqr({"url": "https://www.aqr.com/Insights/Research", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["date"]

    def test_gmo_api_live(self):
        from fetch_articles import fetch_gmo
        articles = fetch_gmo({"url": "https://www.gmo.com/americas/research-library/", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_oaktree_live(self):
        from fetch_articles import fetch_oaktree
        articles = fetch_oaktree({"url": "https://www.oaktreecapital.com/insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_config_valid(self):
        config = json.loads((Path(__file__).parent.parent / "config" / "sources.json").read_text())
        from fetch_articles import FETCHERS
        for src in config["sources"]:
            assert src["id"] in FETCHERS, f"No fetcher for {src['id']}"
            assert "expected_hostname" in src, f"Missing expected_hostname for {src['id']}"
