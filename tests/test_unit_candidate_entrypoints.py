"""Unit tests for candidate entrypoint discovery — file integrity and isolation."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

CANDIDATE_EP_PATH = Path(__file__).parent.parent / "config" / "candidate_entrypoints.json"
PRODUCTION_EP_PATH = Path(__file__).parent.parent / "config" / "entrypoints.json"
CANDIDATES_FILE = Path(__file__).parent.parent / "config" / "fund_candidates.json"

BJT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# File integrity
# ---------------------------------------------------------------------------

def test_candidate_entrypoints_file_exists_and_valid():
    data = json.loads(CANDIDATE_EP_PATH.read_text())
    assert "version" in data
    assert "sources" in data
    assert isinstance(data["sources"], dict)


def test_candidate_entrypoints_isolated_from_production():
    prod = json.loads(PRODUCTION_EP_PATH.read_text())
    cand = json.loads(CANDIDATE_EP_PATH.read_text())
    prod_ids = set(prod["sources"].keys())
    cand_ids = set(cand["sources"].keys())
    overlap = prod_ids & cand_ids
    assert not overlap, f"Candidate entrypoints overlap with production: {overlap}"


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from discover_candidate_entrypoints import (
    score_candidate_page,
    pick_top_entrypoints,
    MAX_ENTRYPOINTS,
    SCORE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# score_candidate_page
# ---------------------------------------------------------------------------

def test_score_candidate_page_research_url():
    """A research-heavy URL with good HTML should score well."""
    html = (
        '<html><body>'
        '<article><time>2026-01-01</time><h2>Report</h2></article>'
        '<article><time>2026-02-01</time><h2>Outlook</h2></article>'
        '<a href="report.pdf">PDF</a>'
        '</body></html>'
    )
    result = score_candidate_page(
        url="https://www.example.com/research/insights",
        html=html,
        allowed_domains=["example.com"],
    )
    assert "final_score" in result
    assert result["final_score"] >= 0.5
    assert result["url"] == "https://www.example.com/research/insights"


def test_score_candidate_page_empty_html():
    """Empty HTML should have structure_score=0 and no gate penalty."""
    result = score_candidate_page(
        url="https://www.example.com/research",
        html="",
        allowed_domains=["example.com"],
    )
    assert result["structure_score"] == 0.0
    assert result["gate_penalty"] == 0.0


def test_score_candidate_page_bad_domain():
    """URL from a non-allowed domain should score 0 on domain component."""
    result = score_candidate_page(
        url="https://www.otherdomain.com/research",
        html="<html><body>content</body></html>",
        allowed_domains=["example.com"],
    )
    assert result["domain_score"] == 0.0


# ---------------------------------------------------------------------------
# pick_top_entrypoints
# ---------------------------------------------------------------------------

def test_pick_top_entrypoints_respects_threshold_and_limit():
    """Only pages >= SCORE_THRESHOLD kept, max MAX_ENTRYPOINTS returned."""
    pages = [
        {"url": f"https://example.com/p{i}", "final_score": score}
        for i, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.3, 0.1])
    ]
    top = pick_top_entrypoints(pages)
    assert len(top) == MAX_ENTRYPOINTS
    assert all(p["final_score"] >= SCORE_THRESHOLD for p in top)
    # First is active, rest are not
    assert top[0]["active"] is True
    assert all(p["active"] is False for p in top[1:])


def test_pick_top_entrypoints_empty_input():
    assert pick_top_entrypoints([]) == []


def test_pick_top_entrypoints_all_below_threshold():
    pages = [
        {"url": "https://example.com/low", "final_score": 0.2},
    ]
    assert pick_top_entrypoints(pages) == []


def test_pick_top_entrypoints_single_above_threshold():
    pages = [
        {"url": "https://example.com/good", "final_score": 0.75},
    ]
    top = pick_top_entrypoints(pages)
    assert len(top) == 1
    assert top[0]["active"] is True


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_constants():
    assert MAX_ENTRYPOINTS == 3
    assert SCORE_THRESHOLD == 0.5
