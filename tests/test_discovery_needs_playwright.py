"""Tests for discovery's shell-HTML / needs_playwright detection.

Without this, candidates whose research_url returns a JS-rendered shell get
scored low → marked LOW quality → skipped by fetcher-synthesis (filter
'quality != LOW') → permanently stuck. The 5-07 reclassification of
franklin-templeton + pinebridge was a manual fix for exactly this bug.

These tests lock the contract: shell HTML routes to needs_playwright without
contaminating the candidate's quality signal.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load discover_candidate_entrypoints
disc_spec = importlib.util.spec_from_file_location(
    "disc_ce", REPO / "discover_candidate_entrypoints.py")
disc = importlib.util.module_from_spec(disc_spec)
sys.modules["disc_ce"] = disc
disc_spec.loader.exec_module(disc)

# Load synthesize_fetchers
sf_spec = importlib.util.spec_from_file_location(
    "sf", REPO / "synthesize_fetchers.py")
sf = importlib.util.module_from_spec(sf_spec)
sys.modules["sf"] = sf
sf_spec.loader.exec_module(sf)


def test_validate_routes_shell_html_to_needs_playwright(monkeypatch):
    """Body too small → needs_playwright, no scoring."""
    candidate = {
        "id": "franklin-templeton-like",
        "name": "Franklin Templeton",
        "research_url": "https://x.example.com/research",
        "official_domain": "example.com",
    }
    # Simulate httpx returning a 2367-char shell (matches real franklin-templeton)
    monkeypatch.setattr(disc, "fetch_page", lambda url: "<html><body>shell</body></html>" * 30)
    weights = {}
    result = disc.validate_candidate(candidate, weights, dry_run=True)
    assert result["needs_playwright"] is True
    assert result["entrypoints"] == []
    assert result["error"] == "shell_html"


def test_validate_routes_no_nav_links_to_needs_playwright(monkeypatch):
    """Body big enough but no extractable links → JS-rendered, needs Playwright."""
    candidate = {
        "id": "pinebridge-like",
        "name": "PineBridge",
        "research_url": "https://x.example.com/insights",
        "official_domain": "example.com",
    }
    big_html = "<html><body>" + ("<p>filler</p>" * 1000) + "</body></html>"
    monkeypatch.setattr(disc, "fetch_page", lambda url: big_html)
    monkeypatch.setattr(disc, "extract_nav_links", lambda *a, **k: [])
    monkeypatch.setattr(disc, "score_candidate_page",
                        lambda *a, **k: {"url": candidate["research_url"], "final_score": 0.4})
    weights = {}
    result = disc.validate_candidate(candidate, weights, dry_run=True)
    assert result["needs_playwright"] is True
    assert result["error"] == "no_nav_links"


def test_validate_normal_path_does_not_set_needs_playwright(monkeypatch):
    """Healthy fetch with nav links → normal scoring path."""
    candidate = {
        "id": "healthy-fund",
        "name": "Healthy",
        "research_url": "https://x.example.com/research",
        "official_domain": "example.com",
    }
    big_html = "<html><body>" + ("<a href='/article-x'>link with padding text</a>" * 200) + "</body></html>"
    assert len(big_html) > disc.SHELL_HTML_THRESHOLD, "test fixture must exceed shell threshold"
    monkeypatch.setattr(disc, "fetch_page", lambda url: big_html)
    monkeypatch.setattr(disc, "extract_nav_links",
                        lambda *a, **k: [{"url": f"https://x.example.com/a/{i}", "label": "x"}
                                         for i in range(5)])
    monkeypatch.setattr(disc, "score_candidate_page",
                        lambda *a, **k: {"url": "https://x.example.com/x", "final_score": 0.7,
                                          "active": True})
    monkeypatch.setattr(disc, "pick_top_entrypoints",
                        lambda pages: [p for p in pages if p["final_score"] >= 0.5])
    weights = {}
    result = disc.validate_candidate(candidate, weights, dry_run=True)
    assert result.get("needs_playwright") is None or result["needs_playwright"] is False
    assert result["entrypoints"] != []


def test_threshold_constant_is_reasonable():
    """Threshold should be high enough to catch known shells (2367 chars) but not
    so high that small but valid index pages trip falsely."""
    assert disc.SHELL_HTML_THRESHOLD >= 2500  # franklin-templeton was 2367
    assert disc.SHELL_HTML_THRESHOLD < 50000  # don't false-positive medium pages


# ── synthesize_fetchers.py: needs_playwright sort priority ─────────────────────

def test_synthesize_fetchers_prioritises_needs_playwright(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps([
        {"id": "ssr-fund", "status": "inaccessible", "quality": "MEDIUM"},
        {"id": "playwright-needed", "status": "inaccessible", "quality": "MEDIUM",
         "needs_playwright": True},
        {"id": "another-ssr", "status": "inaccessible", "quality": "HIGH"},
    ]))
    monkeypatch.setattr(sf, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sf, "load_fetcher_ids", lambda: set())
    targets = sf.list_targets()
    ids = [t["id"] for t in targets]
    # needs_playwright candidate must come first
    assert ids[0] == "playwright-needed"
    assert "ssr-fund" in ids
    assert "another-ssr" in ids


def test_synthesize_fetchers_quality_secondary_sort(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps([
        {"id": "low-quality", "status": "inaccessible", "quality": "MEDIUM"},
        {"id": "high-quality", "status": "inaccessible", "quality": "HIGH"},
    ]))
    monkeypatch.setattr(sf, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sf, "load_fetcher_ids", lambda: set())
    targets = sf.list_targets()
    ids = [t["id"] for t in targets]
    # Among non-playwright candidates, HIGH quality first
    assert ids == ["high-quality", "low-quality"]


def test_synthesize_fetchers_emits_needs_playwright_field(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps([
        {"id": "x", "status": "inaccessible", "quality": "HIGH",
         "needs_playwright": True},
    ]))
    monkeypatch.setattr(sf, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sf, "load_fetcher_ids", lambda: set())
    targets = sf.list_targets()
    assert targets[0]["needs_playwright"] is True
