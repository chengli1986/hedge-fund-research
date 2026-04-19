"""Test scaffolding for Fetcher Synthesis pipeline."""

import pytest


def test_injection_marker_exists_in_fetch_articles():
    """The marker comment must exist exactly once in fetch_articles.py."""
    text = open("fetch_articles.py").read()
    assert text.count("# FETCHER_SYNTHESIS_INSERTION_POINT") == 1
