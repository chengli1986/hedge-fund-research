"""HTML from a browser must be checked for a challenge page, once.

Audit A2. The challenge check lives inside _normalize_html, so it only
runs for extractors that parse through it. oaktree does neither: it drives
its own browser (no requests response for fetch_with_evidence to record)
and parses the page itself (never reaching _normalize_html), so a
Cloudflare interstitial there is invisible to both mechanisms and the
failure reads as a PDF problem.

The fix is a choke point: every browser-driven extractor takes its HTML
through _page_html(), which runs the same check. Seven of the eight also
call _normalize_html afterwards, so the check has to be idempotent -- one
hint, one recorded path, however many times the same HTML is examined.
"""
import ast
from pathlib import Path

import pytest

import failure_labels as fl
import fetch_content as fc

REPO = Path(__file__).resolve().parent.parent
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body>Enable JavaScript and cookies to continue</body></html>")
ARTICLE = "<html><body><main><p>" + ("Rates fell. " * 50) + "</p></main></body></html>"


class _Page:
    def __init__(self, html):
        self._html = html

    def content(self):
        return self._html


@pytest.fixture(autouse=True)
def _clean():
    fc._failure_hints.clear()
    fc.drain_extraction_paths()
    yield
    fc._failure_hints.clear()
    fc.drain_extraction_paths()


class TestTheCheckAtTheEntryPoint:
    def test_a_challenge_page_is_noted_when_the_browser_hands_it_over(self):
        fc._page_html(_Page(CHALLENGE))
        assert ("blocked_by_bot_protection", ) == tuple(l for l, _ in fc._failure_hints)

    def test_an_article_is_not_noted(self):
        fc._page_html(_Page(ARTICLE))
        assert fc._failure_hints == [] and fc.drain_extraction_paths() == []

    def test_the_html_comes_back_unchanged(self):
        assert fc._page_html(_Page(ARTICLE)) == ARTICLE

    def test_examining_the_same_page_twice_notes_it_once(self):
        """Seven of the eight then pass the same HTML to _normalize_html."""
        html = fc._page_html(_Page(CHALLENGE))
        fc._normalize_html(html, "main p")
        assert [l for l, _ in fc._failure_hints] == ["blocked_by_bot_protection"]
        assert fc.drain_extraction_paths().count("challenge") == 1

    def test_the_classifier_reaches_blocked_for_a_browser_only_extractor(self):
        """oaktree's shape: browser HTML, no requests response, own parsing."""
        fc._page_html(_Page(CHALLENGE))
        evidence = {"exception": None, "messages": [], "responses": [],
                    "extraction_paths": fc.drain_extraction_paths(),
                    "hints": list(fc._failure_hints)}
        assert fl.classify_content_failure(evidence)[0] == "blocked_by_bot_protection"


class TestNoExtractorSlipsPast:
    """A choke point that anything may bypass is not one. This is the guard
    that stops the next extractor from reintroducing A2."""

    def test_no_extractor_calls_page_content_directly(self):
        tree = ast.parse((REPO / "fetch_content.py").read_text())
        offenders = []
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef) and node.name != "_page_html"):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "content"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "page"):
                    offenders.append(node.name)
        assert offenders == [], f"these must go through _page_html: {sorted(set(offenders))}"
