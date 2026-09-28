"""The six bespoke HTML extractors must be able to say "the rule missed".

Audit A3. selector_miss -- "the extraction rule matched nothing, we fell
back" -- is how a site redesign announces itself, and the classifier reads
it from the extraction paths that _normalize_html records. Nine extractors
parse without going through it and record nothing, so they can never carry
that label: the store holds zero selector_miss rows, not because no site
has been redesigned but because these nine cannot report it.

Three of the nine (gmo, oaktree, pdf_url) look for a PDF rather than
article text, where "the selector missed" is not the right sentence. The
six that do select article text now record, in the same vocabulary the
shared path uses.
"""
import ast
from pathlib import Path

import pytest

import failure_labels as fl
import fetch_content as fc

REPO = Path(__file__).resolve().parent.parent
BESPOKE_HTML_EXTRACTORS = ("robeco", "de_shaw", "gsam", "metlife_im",
                           "matthews_asia", "bridgewater")


@pytest.fixture(autouse=True)
def _clean():
    fc.drain_extraction_paths()
    fc._failure_hints.clear()
    yield
    fc.drain_extraction_paths()
    fc._failure_hints.clear()


class TestTheHelper:
    def test_a_hit_records_the_same_word_the_shared_path_uses(self):
        fc._note_selector_result(True)
        assert fc.drain_extraction_paths() == ["primary"]

    def test_a_miss_records_something_the_classifier_reads_as_drift(self):
        fc._note_selector_result(False)
        paths = fc.drain_extraction_paths()
        assert paths and all(p != "primary" for p in paths)

    def test_a_miss_reaches_the_selector_miss_label(self):
        fc._note_selector_result(False)
        evidence = {"exception": None, "messages": [], "responses": [],
                    "extraction_paths": fc.drain_extraction_paths(), "hints": []}
        assert fl.classify_content_failure(evidence)[0] == "selector_miss"


class TestRobecoEndToEnd:
    """One extractor all the way through, rather than only the helper."""

    def _fetch(self, monkeypatch, tmp_path, html):
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        resp = type("R", (), {"status_code": 200, "text": html,
                              "headers": {"Content-Type": "text/html"},
                              "url": "https://www.robeco.com/a",
                              "raise_for_status": lambda self: None})()
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        return fc.fetch_with_evidence({"id": "r1", "url": "https://www.robeco.com/a"},
                                      fc._fetch_content_robeco)

    def test_a_redesigned_page_is_labelled_selector_miss(self, monkeypatch, tmp_path):
        """No <main> at all: the site moved its article body elsewhere."""
        html = "<html><body><div class='new-layout'><p>" + ("Text. " * 80) + "</p></div></body></html>"
        result, evidence = self._fetch(monkeypatch, tmp_path, html)
        assert result is None
        assert fl.classify_content_failure(evidence)[0] == "selector_miss"

    def test_a_working_page_records_a_hit_and_no_drift(self, monkeypatch, tmp_path):
        html = "<html><body><main><p>" + ("Rates fell. " * 80) + "</p></main></body></html>"
        result, evidence = self._fetch(monkeypatch, tmp_path, html)
        assert result is not None
        assert evidence["extraction_paths"] == ["primary"]


class TestAllSixReport:
    """A guard, so the next bespoke extractor cannot quietly go silent."""

    def test_every_bespoke_html_extractor_records_its_selector_result(self):
        src = (REPO / "fetch_content.py").read_text()
        tree = ast.parse(src)
        bodies = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                bodies[node.name] = ast.get_source_segment(src, node)
        missing = []
        for name in BESPOKE_HTML_EXTRACTORS:
            body = bodies.get(f"_fetch_content_{name}", "")
            if name == "bridgewater":
                body += bodies.get("_extract_bridgewater_text", "")
            if "_note_selector_result(" not in body and "_normalize_html(" not in body:
                missing.append(name)
        assert missing == [], f"these cannot report a redesign: {missing}"


class TestBridgewaterLadder:
    """Its 18-selector ladder returns None for three different reasons, and
    only one of them is a redesign. Labelling a short body or a login wall
    as selector_miss would send the reader to edit selectors that are fine.

    The source-level guard above cannot see this: it passes as long as the
    call appears somewhere in the function.
    """

    def _evidence(self, html):
        fc.drain_extraction_paths()
        fc._failure_hints.clear()
        fc._extract_bridgewater_text(html)
        return {"exception": None, "messages": [], "responses": [],
                "extraction_paths": fc.drain_extraction_paths(),
                "hints": list(fc._failure_hints)}

    def test_no_selector_matching_is_a_redesign(self):
        html = "<html><body><div class='brand-new'><p>" + ("Text. " * 80) + "</p></div></body></html>"
        assert fl.classify_content_failure(self._evidence(html))[0] == "selector_miss"

    def test_a_body_that_was_found_but_is_too_short_is_not_a_redesign(self):
        html = "<html><body><div class='RichTextBody'>Too short.</div></body></html>"
        assert fl.classify_content_failure(self._evidence(html))[0] != "selector_miss"

    def test_a_login_wall_is_not_a_redesign(self):
        html = ("<html><body><div class='RichTextBody'>"
                + "Subscribe to read the full note. " * 12 + "</div></body></html>")
        assert fl.classify_content_failure(self._evidence(html))[0] == "access_denied"

    def test_a_good_article_records_a_hit(self):
        html = ("<html><body><div class='RichTextBody'>"
                + "Rates fell in September. " * 40 + "</div></body></html>")
        ev = self._evidence(html)
        assert ev["extraction_paths"] == ["primary"]
