"""Test scaffolding for Fetcher Synthesis pipeline."""

from pathlib import Path

FETCH_ARTICLES = Path(__file__).resolve().parent.parent / "fetch_articles.py"
MARKER = "# FETCHER_SYNTHESIS_INSERTION_POINT"


def test_injection_marker_exists_exactly_once():
    """The marker comment must exist exactly once in fetch_articles.py."""
    text = FETCH_ARTICLES.read_text(encoding="utf-8")
    assert text.count(MARKER) == 1


def test_injection_marker_is_before_dispatcher():
    """The marker must appear before the Dispatcher comment block."""
    text = FETCH_ARTICLES.read_text(encoding="utf-8")
    marker_pos = text.find(MARKER)
    dispatcher_pos = text.find("# Dispatcher")
    assert marker_pos != -1, "Marker not found"
    assert dispatcher_pos != -1, "Dispatcher block not found"
    assert marker_pos < dispatcher_pos, "Marker must appear before Dispatcher"


# Tests for synthesize_fetchers.py
import json
import sys
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
import synthesize_fetchers


def _make_candidate(id_, status, quality, attempted_at=None):
    c = {"id": id_, "name": id_, "status": status, "quality": quality,
         "homepage_url": f"https://{id_}.com", "research_url": None, "notes": ""}
    if attempted_at:
        c["synthesis_attempted_at"] = attempted_at
    return c


def test_list_targets_returns_only_inaccessible():
    candidates = [
        _make_candidate("alpha", "inaccessible", "HIGH"),
        _make_candidate("beta",  "validated",    "HIGH"),
        _make_candidate("gamma", "rejected",     "HIGH"),
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]


def test_list_targets_excludes_low_quality():
    candidates = [
        _make_candidate("alpha", "inaccessible", "HIGH"),
        _make_candidate("beta",  "inaccessible", "LOW"),
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]


def test_list_targets_excludes_already_has_fetcher():
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH")]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value={"alpha"}):
        result = synthesize_fetchers.list_targets()
    assert result == []


def test_list_targets_excludes_recently_attempted():
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=recent)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert result == []


def test_list_targets_includes_stale_attempt():
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=stale)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]
