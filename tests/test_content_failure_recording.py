"""Stage 2 records a labelled failure with its evidence (2026-09-15).

Each failed fetch stores content_failure = {label, detail, at, attempt} on the
article and appends the same to logs/content-failures.jsonl, so failures can
be counted by source and label over time. Evidence is gathered around the
fetcher without changing it: HTTP responses made through requests (status,
final URL after redirects, challenge / media-player markers), warnings the
fetcher logged, the extraction path, and explicit hints a fetcher records.
"""
import json
import sys

import pytest
import requests

import fetch_content as fc


def _fake_adapter(monkeypatch, pages):
    """Serve `pages` {url: (status, html, final_url)} through the real requests stack."""
    def send(self, request, **kw):
        status, html, final = pages[request.url]
        r = requests.Response()
        r.status_code = status
        r._content = html.encode()
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        r.url = final or request.url
        r.request = request
        return r
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)


def test_http_evidence_is_recorded_through_requests_get(monkeypatch):
    url = "https://www.ark-invest.com/articles/analyst-research/x"
    _fake_adapter(monkeypatch, {url: (403, "<title>Just a moment...</title>", None)})
    article = {"id": "a1", "source_id": "ark-invest", "title": "X", "url": url}
    result, evidence = fc.fetch_with_evidence(article, lambda a: (requests.get(a["url"]), None)[1])
    assert result is None
    assert evidence["responses"][0]["status"] == 403 and evidence["responses"][0]["challenge"] is True


def test_warnings_and_extraction_paths_are_collected():
    def fetcher(a):
        fc._normalize_html("<main><p>short</p></main>", ".gone p")
        fc.log.warning("  X: extracted text too short (5 chars)")
        return None
    _, evidence = fc.fetch_with_evidence({"id": "a2", "url": "https://x/y"}, fetcher)
    assert evidence["extraction_paths"] == ["fallback:main"]
    assert any("too short" in m for m in evidence["messages"])


def test_an_exception_is_evidence_not_a_crash():
    def fetcher(a):
        raise ValueError("boom")
    result, evidence = fc.fetch_with_evidence({"id": "a3", "url": "https://x/y"}, fetcher)
    assert result is None and "boom" in evidence["exception"]


def test_hints_are_collected():
    def fetcher(a):
        fc.note_failure_hint("page_gone", "soft 404")
        return None
    _, evidence = fc.fetch_with_evidence({"id": "a4", "url": "https://x/y"}, fetcher)
    assert evidence["hints"] == [("page_gone", "soft 404")]


class TestMainRecordsLabels:
    def _run(self, tmp_path, monkeypatch, fetcher, article=None):
        data = tmp_path / "articles.jsonl"
        art = article or {"id": "m1", "source_id": "matthews-asia", "title": "Video", "url": "https://x/v",
                          "summarized": False}
        data.write_text(json.dumps(art) + "\n")
        monkeypatch.setattr(fc, "DATA_FILE", data)
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "content")
        monkeypatch.setattr(fc, "BASE_DIR", tmp_path)          # content_path is stored relative to it
        monkeypatch.setitem(fc.CONTENT_FETCHERS, art["source_id"], fetcher)
        monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
        fc.main()
        return json.loads(data.read_text().splitlines()[0])

    def test_a_failure_is_labelled_on_the_article_and_in_the_ledger(self, tmp_path, monkeypatch):
        def fetcher(a):
            fc.note_failure_hint("media_without_text", "video page")
            return None
        row = self._run(tmp_path, monkeypatch, fetcher)
        assert row["content_failure"]["label"] == "media_without_text"
        assert row["content_failure"]["attempt"] == 1
        ledger = [json.loads(l) for l in fc.CONTENT_FAILURE_LOG.read_text().splitlines()]
        assert ledger[-1]["id"] == "m1" and ledger[-1]["label"] == "media_without_text"
        assert ledger[-1]["source_id"] == "matthews-asia"

    def test_success_clears_the_last_failure(self, tmp_path, monkeypatch):
        art = {"id": "m2", "source_id": "matthews-asia", "title": "T", "url": "https://x/t", "summarized": False,
               "content_status": "failed", "content_attempts": 1,
               "content_failure": {"label": "fetch_error", "detail": "timeout"}}
        def fetcher(a):
            p = fc.CONTENT_DIR / f"{a['id']}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("body " * 100)
            return (p, "ok")
        row = self._run(tmp_path, monkeypatch, fetcher, art)
        assert row["content_status"] == "ok" and "content_failure" not in row

    def test_the_ledger_path_is_redirected_in_tests(self):
        from conftest import PRODUCTION_PATHS
        assert fc.CONTENT_FAILURE_LOG != PRODUCTION_PATHS["CONTENT_FAILURE_LOG"]
