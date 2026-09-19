"""Stage 2 audit (2026-09-19): fetch_content.py generic machinery.

F6 -- CODE_VERSION was the hash of the whole 2455-line file, and a
      code-dependent permafail is re-tried once whenever the version it failed
      under differs from the current one. fetch_content.py is committed most
      days (4/5/6/2/1/1 commits on 09-13..09-18), so "once per change" was
      "nightly": the same 7 gsam articles were re-fetched and re-retired four
      nights running (ledger attempt 6 -> 9), each night counted as 7 real
      failures in "Content fetch complete: N ok, 7 failed", and their
      content_permafailed_at was re-stamped so "retired since" was always
      yesterday. The version is now per source: the shared machinery plus
      that source's own fetcher, so an edit to another source's fetcher does
      not requeue this one.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_content as fc

BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=BJT)
REPO = Path(__file__).resolve().parent.parent


def _variant(tmp_path, old: str, new: str):
    """Import a copy of fetch_content.py with one edit applied."""
    src = (REPO / "fetch_content.py").read_text(encoding="utf-8")
    assert src.count(old) == 1, old
    p = tmp_path / "fetch_content.py"
    p.write_text(src.replace(old, new), encoding="utf-8")
    (tmp_path / "config").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location(f"fc_variant_{abs(hash(new))}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _art(source_id, **kw):
    return dict({"id": "x", "source_id": source_id, "summarized": False, "url": "https://x/a"}, **kw)


class TestF6PerSourceCodeVersion:
    def test_editing_another_sources_fetcher_does_not_change_this_ones_version(self, tmp_path):
        variant = _variant(tmp_path, '"""Fetch GMO article content: download PDF, extract text with pdfplumber."""',
                           '"""Fetch GMO article content: download PDF, extract text with pdfplumber. (edited)"""')
        oaktree = _art("oaktree")
        assert variant.code_version_for(oaktree) == fc.code_version_for(oaktree)

    def test_editing_a_sources_own_fetcher_changes_its_version(self, tmp_path):
        variant = _variant(tmp_path, '"""Fetch GMO article content: download PDF, extract text with pdfplumber."""',
                           '"""Fetch GMO article content: download PDF, extract text with pdfplumber. (edited)"""')
        gmo = _art("gmo")
        assert variant.code_version_for(gmo) != fc.code_version_for(gmo)

    def test_editing_shared_code_changes_every_version(self, tmp_path):
        variant = _variant(tmp_path, "def _normalize_html(html: str, selector: str) -> str:",
                           "def _normalize_html(html: str, selector: str) -> str:\n    # edited")
        assert variant.code_version_for(_art("oaktree")) != fc.code_version_for(_art("oaktree"))
        assert variant.code_version_for(_art("gmo")) != fc.code_version_for(_art("gmo"))

    def test_a_permafail_under_this_sources_version_is_not_requeued(self):
        a = _art("gmo", content_status="permafail", content_attempts=3)
        a["content_failure"] = {"label": "selector_miss", "detail": "d",
                                "code_version": fc.code_version_for(a)}
        assert not fc.is_content_pending(a)

    def test_a_failure_records_this_sources_version(self):
        a = _art("gmo")
        fc.mark_content_failure(a, failure={"label": "selector_miss", "detail": "d"}, now=NOW)
        assert a["content_failure"]["code_version"] == fc.code_version_for(a)


class TestF6ReRetirementIsNotANewRetirement:
    def test_permafailed_at_keeps_the_first_retirement(self):
        a = _art("gmo")
        for i in range(3):
            fc.mark_content_failure(a, failure={"label": "selector_miss", "detail": "d"},
                                    now=NOW + timedelta(days=i))
        assert a["content_status"] == "permafail"
        first = a["content_permafailed_at"]
        fc.mark_content_failure(a, failure={"label": "selector_miss", "detail": "d"},
                                now=NOW + timedelta(days=30))
        assert a["content_status"] == "permafail"
        assert a["content_permafailed_at"] == first

    def test_the_summary_separates_code_change_retries_from_failures(self, tmp_path, monkeypatch, capsys):
        src = next(iter(fc.CONTENT_FETCHERS))
        stale = _art(src, id="r1", title="T", content_status="permafail", content_attempts=5,
                     content_failure={"label": "selector_miss", "detail": "d", "code_version": "old"})
        fresh = _art(src, id="n1", title="U")
        data = tmp_path / "articles.jsonl"
        data.write_text(json.dumps(stale) + "\n" + json.dumps(fresh) + "\n")
        monkeypatch.setattr(fc, "DATA_FILE", data)
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "c")
        monkeypatch.setitem(fc.CONTENT_FETCHERS, src, lambda a: None)
        monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
        fc.main()
        out = capsys.readouterr().out
        assert "Failed: 2" in out
        assert "re-tried after a code change: 1" in out
