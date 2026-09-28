"""The safety rope for changing a content extractor.

Stage 1 has scripts/compare_fetchers.py: before a listing moves to a
template, the two are run against the live site and must return the same
titles, urls and dates. Stage 2 has had no equivalent, which is why the
2026-09-26 template switch silently dropped two fields ARK needed.

This is that rope for bodies: re-extract stored articles with the current
code and require the text to be identical, character for character. The
weekly audit (scripts/content_audit.py) already re-fetches into a temp
directory and this reuses its helper, but its verdict is deliberately
fuzzy -- it drops punctuation and compares shingle overlap, so a site's own
edit does not read as a regression. Proving a refactor changed nothing
needs the strict comparison instead.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "compare_extractors", REPO / "scripts" / "compare_extractors.py")
ce = importlib.util.module_from_spec(spec)
sys.modules["compare_extractors"] = ce
spec.loader.exec_module(ce)

BODY = "Rates fell in September as growth cooled. " * 20


def _article(tmp_path, text=BODY, aid="a1"):
    (tmp_path / f"{aid}.txt").write_text(text, encoding="utf-8")
    return {"id": aid, "source_id": "s1", "url": "https://s1.test/a",
            "title": "T", "content_path": f"{tmp_path.name}/{aid}.txt",
            "content_status": "ok"}


class TestOneArticle:
    def _check(self, tmp_path, monkeypatch, new_text, fails=False):
        art = _article(tmp_path)
        monkeypatch.setattr(ce, "_fetch_to_temp",
                            lambda a, f: ((None, "ok") if fails else (object(), "ok"),
                                          {"messages": [], "responses": [],
                                           "extraction_paths": [], "hints": [],
                                           "exception": None},
                                          new_text))
        return ce.compare_article(art, fetcher=None, stored_dir=tmp_path)

    def test_identical_text_passes(self, tmp_path, monkeypatch):
        out = self._check(tmp_path, monkeypatch, BODY)
        assert out["same"] is True and out["stored_chars"] == out["new_chars"]

    def test_one_changed_character_fails(self, tmp_path, monkeypatch):
        out = self._check(tmp_path, monkeypatch, BODY.replace("September", "October", 1))
        assert out["same"] is False and out["first_diff"] is not None

    def test_whitespace_alone_still_counts_as_different(self, tmp_path, monkeypatch):
        """The fuzzy weekly audit forgives this; a refactor must not cause it."""
        out = self._check(tmp_path, monkeypatch, BODY.replace(" ", "  ", 1))
        assert out["same"] is False

    def test_a_failed_fetch_is_reported_not_counted_as_same(self, tmp_path, monkeypatch):
        out = self._check(tmp_path, monkeypatch, "", fails=True)
        assert out["same"] is False and out["failed"] is True

    def test_a_missing_stored_file_is_skipped_not_failed(self, tmp_path, monkeypatch):
        art = {"id": "gone", "source_id": "s1", "url": "u",
               "content_path": "content/does-not-exist.txt", "content_status": "ok"}
        monkeypatch.setattr(ce, "_fetch_to_temp", lambda a, f: (None, {}, ""))
        assert ce.compare_article(art, fetcher=None, stored_dir=tmp_path)["skipped"] == "no stored body"


class TestRunVerdict:
    def _run(self, results):
        return ce.verdict(results)

    def test_all_identical_is_a_pass(self):
        assert self._run([{"same": True}, {"same": True}]) == 0

    def test_any_difference_fails_the_run(self):
        assert self._run([{"same": True}, {"same": False}]) == 1

    def test_a_failed_fetch_fails_the_run(self):
        assert self._run([{"same": False, "failed": True}]) == 1

    def test_skipped_articles_do_not_fail_it(self):
        assert self._run([{"same": True}, {"skipped": "no stored body"}]) == 0

    def test_nothing_compared_is_not_a_silent_pass(self):
        """An empty run must not read as "everything matched"."""
        assert self._run([]) == 2
