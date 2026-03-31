import json
import pytest
from pathlib import Path
from urllib.parse import urlparse

pytestmark = pytest.mark.nightly

def _fetch_all_sources():
    config = json.loads((Path(__file__).parent.parent / "config" / "sources.json").read_text())
    from fetch_articles import FETCHERS
    results = {}
    for src in config["sources"]:
        fetcher = FETCHERS.get(src["id"])
        if fetcher:
            try:
                results[src["id"]] = fetcher(src)
            except Exception:
                results[src["id"]] = []
    return results, {s["id"]: s for s in config["sources"]}

@pytest.fixture(scope="module")
def all_sources():
    return _fetch_all_sources()

class TestArticleCounts:
    def test_man_group_count(self, all_sources):
        arts, _ = all_sources
        assert 3 <= len(arts.get("man-group", [])) <= 10

    def test_bridgewater_count(self, all_sources):
        arts, _ = all_sources
        assert 3 <= len(arts.get("bridgewater", [])) <= 20

    def test_aqr_count(self, all_sources):
        arts, _ = all_sources
        assert 5 <= len(arts.get("aqr", [])) <= 15

    def test_gmo_count(self, all_sources):
        arts, _ = all_sources
        assert 5 <= len(arts.get("gmo", [])) <= 15

    def test_oaktree_count(self, all_sources):
        arts, _ = all_sources
        assert 5 <= len(arts.get("oaktree", [])) <= 20

class TestDataQuality:
    def test_all_have_titles(self, all_sources):
        arts, _ = all_sources
        for src_id, articles in arts.items():
            for a in articles:
                assert a.get("title"), f"{src_id}: article missing title"

    def test_date_coverage_80pct(self, all_sources):
        arts, _ = all_sources
        total = sum(len(v) for v in arts.values())
        with_dates = sum(1 for articles in arts.values() for a in articles if a.get("date"))
        assert total > 0
        assert with_dates / total >= 0.8

    def test_no_cross_source_contamination(self, all_sources):
        arts, sources = all_sources
        for src_id, articles in arts.items():
            expected_host = sources[src_id].get("expected_hostname", "")
            if not expected_host:
                continue
            for a in articles:
                hostname = urlparse(a["url"]).hostname or ""
                assert hostname.endswith(expected_host), f"{src_id}: {a['url']} doesn't match {expected_host}"

class TestApiHealth:
    def test_gmo_api_returns_json(self):
        import requests
        from bs4 import BeautifulSoup
        resp = requests.get("https://www.gmo.com/americas/research-library/",
                           cookies={"GMO_region": "NorthAmerica"}, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        grid = soup.select_one("section.article-grid[data-endpoint]")
        assert grid, "GMO article-grid not found"
        api_url = "https://www.gmo.com" + grid["data-endpoint"] + "&currentPage=1"
        api_resp = requests.get(api_url, cookies={"GMO_region": "NorthAmerica"}, timeout=30)
        assert api_resp.status_code == 200
        data = api_resp.json()
        assert "listing" in data
