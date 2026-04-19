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
