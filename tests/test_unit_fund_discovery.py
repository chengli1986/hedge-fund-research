"""Unit tests for fund discovery seed pool and candidate state model."""

import json
import pytest
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"
SEED_FILE = CONFIG_DIR / "fund_seeds.json"
CANDIDATES_FILE = CONFIG_DIR / "fund_candidates.json"
SOURCES_FILE = CONFIG_DIR / "sources.json"

VALID_STATUSES = {
    "seed", "discovered", "screened", "screen_failed", "validated", "inaccessible",
    "watchlist", "rejected", "promoted",
}


# ---------------------------------------------------------------------------
# Seed file tests
# ---------------------------------------------------------------------------

class TestSeedFile:
    def test_seed_file_is_valid_json(self):
        """fund_seeds.json must be valid JSON."""
        text = SEED_FILE.read_text()
        seeds = json.loads(text)
        assert isinstance(seeds, list)

    def test_seeds_have_required_fields(self):
        """Every seed must have id, name, category, homepage."""
        seeds = json.loads(SEED_FILE.read_text())
        required = {"id", "name", "category", "homepage"}
        for seed in seeds:
            missing = required - set(seed.keys())
            assert not missing, f"Seed {seed.get('id', '?')} missing: {missing}"

    def test_seed_ids_are_unique(self):
        """Seed IDs must be unique."""
        seeds = json.loads(SEED_FILE.read_text())
        ids = [s["id"] for s in seeds]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_no_overlap_with_production_sources(self):
        """Seed IDs must not overlap with production source IDs."""
        seeds = json.loads(SEED_FILE.read_text())
        sources = json.loads(SOURCES_FILE.read_text())
        seed_ids = {s["id"] for s in seeds}
        source_ids = {s["id"] for s in sources["sources"]}
        overlap = seed_ids & source_ids
        assert not overlap, f"Overlap with production: {overlap}"


# ---------------------------------------------------------------------------
# Candidate file tests
# ---------------------------------------------------------------------------

class TestCandidatesFile:
    def test_candidates_file_is_valid_json(self):
        """fund_candidates.json must be valid JSON."""
        text = CANDIDATES_FILE.read_text()
        candidates = json.loads(text)
        assert isinstance(candidates, list)

    def test_candidates_have_required_fields(self):
        """Every candidate must have id, name, status."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        required = {"id", "name", "status"}
        for c in candidates:
            missing = required - set(c.keys())
            assert not missing, f"Candidate {c.get('id', '?')} missing: {missing}"

    def test_candidate_statuses_are_valid(self):
        """Every candidate status must be in the valid set."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        for c in candidates:
            assert c["status"] in VALID_STATUSES, (
                f"Candidate {c['id']} has invalid status: {c['status']}"
            )

    def test_all_seeds_have_candidate_entry(self):
        """Every seed must have a corresponding candidate entry."""
        seeds = json.loads(SEED_FILE.read_text())
        candidates = json.loads(CANDIDATES_FILE.read_text())
        seed_ids = {s["id"] for s in seeds}
        candidate_ids = {c["id"] for c in candidates}
        missing = seed_ids - candidate_ids
        assert not missing, f"Seeds without candidate entry: {missing}"


# ---------------------------------------------------------------------------
# discover_fund_sites tests
# ---------------------------------------------------------------------------

import discover_fund_sites as dfs


class TestExtractResearchLinks:
    def test_extract_research_links_finds_insights(self):
        html = '<html><body><nav><a href="/insights">Insights</a><a href="/about">About Us</a><a href="/research">Research</a><a href="/careers">Careers</a></nav></body></html>'
        links = dfs.extract_research_links(html, "https://example.com", ["example.com"])
        urls = [l["url"] for l in links]
        assert "https://example.com/insights" in urls
        assert "https://example.com/research" in urls
        assert "https://example.com/careers" not in urls

    def test_extract_research_links_filters_negative_paths(self):
        html = '<html><body><a href="/perspectives">Perspectives</a><a href="/login">Login</a><a href="/subscribe">Subscribe</a><a href="/white-papers">White Papers</a></body></html>'
        links = dfs.extract_research_links(html, "https://example.com", ["example.com"])
        urls = [l["url"] for l in links]
        assert "https://example.com/perspectives" in urls
        assert "https://example.com/white-papers" in urls
        assert "https://example.com/login" not in urls
        assert "https://example.com/subscribe" not in urls


class TestDetectRss:
    def test_detect_rss_finds_feed_links(self):
        html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml" /></head><body></body></html>'
        feeds = dfs.detect_rss(html, "https://example.com")
        assert len(feeds) == 1
        assert feeds[0] == "https://example.com/feed.xml"

    def test_detect_rss_returns_empty_when_none(self):
        html = '<html><head></head><body></body></html>'
        feeds = dfs.detect_rss(html, "https://example.com")
        assert feeds == []


# ---------------------------------------------------------------------------
# screen_fund_candidates tests
# ---------------------------------------------------------------------------

import screen_fund_candidates as sfc


class TestScreenPage:
    def test_screen_rejects_non_public_page(self):
        result = sfc.screen_page("https://example.com/research", status_code=403, html="")
        assert result["passed"] is False
        assert "not publicly accessible" in result["reason"].lower()

    def test_screen_rejects_login_page(self):
        html = '<html><body><form><input type="password" /><button>Log In</button></form></body></html>'
        result = sfc.screen_page("https://example.com/insights", status_code=200, html=html)
        assert result["passed"] is False
        assert "login" in result["reason"].lower()

    def test_screen_passes_research_index(self):
        html = '<html><body><article><h2>Q1 Outlook</h2><time>2026-03-15</time></article><article><h2>Market Commentary</h2><time>2026-03-01</time></article><article><h2>Investment Perspectives</h2><time>2026-02-15</time></article></body></html>'
        result = sfc.screen_page("https://example.com/insights", status_code=200, html=html)
        assert result["passed"] is True

    def test_screen_rejects_single_article(self):
        html = '<html><body><article><h1>Our 2026 Outlook</h1><p>Long article content here...</p></article></body></html>'
        result = sfc.screen_page("https://example.com/insights/outlook-2026", status_code=200, html=html)
        assert result["passed"] is False
