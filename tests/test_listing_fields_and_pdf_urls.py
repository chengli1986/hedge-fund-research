"""Fields content fetchers need, and articles whose URL is a PDF (2026-09-15).

Listing fields were dropped. fetch_source stored a fixed set of keys, so the
"summary"/"category" a listing produced (ARK's RSS fallback) and gsam's
"gsam_summary" (its SPA fallback) never reached the stored row: not one of 49
gsam rows had the key. Both fallbacks were dead code; seven gsam pages whose
body is rendered client-side were permafail with "summary 0 chars". The
fields content fetchers read are now kept, and a structural test ties the
two files together so a new fallback cannot silently go dead again.

A listing can point straight at a PDF (de-shaw's "Divergent Interests",
oaktree's press release). The per-source fetchers parsed it as HTML
(0 chars) or opened it in a browser ("Download is starting"). Any article
whose URL path ends in .pdf is now read as a PDF.

gsam's API summary stands in for a body the page does not serve; it is now
stored as metadata_only, like ARK's, so stage 3 treats it as limited.
"""
import ast
import inspect
from unittest.mock import MagicMock

import pytest

import fetch_articles as fa
import fetch_content as fc


def test_listing_fields_reach_the_stored_row(monkeypatch):
    listed = [{"title": "Private Equity Co-Investments", "url": "https://am.gs.com/x", "date": "2026-09-14",
               "gsam_summary": "The co-investment market is expanding.", "summary": "s", "category": "c"}]
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: listed)
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    src = {"id": "gsam", "name": "GSAM", "short_name": "GSAM", "method": "api", "url": "https://x/",
           "expected_hostname": ""}
    row = fa.fetch_source(src, set())[0]
    assert row["gsam_summary"] == "The co-investment market is expanding."
    assert row["summary"] == "s" and row["category"] == "c"


def test_absent_listing_fields_are_not_invented(monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: [{"title": "T", "url": "https://am.gs.com/y", "date": "2026-09-14"}])
    monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
    src = {"id": "gsam", "name": "GSAM", "short_name": "GSAM", "method": "api", "url": "https://x/",
           "expected_hostname": ""}
    row = fa.fetch_source(src, set())[0]
    assert not {"gsam_summary", "summary", "category"} & set(row)


def test_every_article_field_a_content_fetcher_reads_is_stored():
    """Structural: collect article.get("x") / article["x"] in fetch_content and
    require each to be a key fetch_source writes, a listing field it keeps, or
    one the pipeline itself maintains."""
    tree = ast.parse(inspect.getsource(fc))
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "article"
                and node.args and isinstance(node.args[0], ast.Constant)):
            read.add(node.args[0].value)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "article"
                and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)
    written_by_fetch_source = {"id", "source_id", "source_name", "title", "url", "date", "date_raw",
                               "fetched_at", "summarized"}
    pipeline_managed = {"content_status", "content_path", "content_attempts", "content_permafailed_at"}
    missing = read - written_by_fetch_source - set(fa.LISTING_FIELDS_KEPT) - pipeline_managed
    assert missing == set(), f"content fetchers read fields that are never stored: {missing}"


class TestPdfUrl:
    def _run(self, tmp_path, monkeypatch, url, text="Divergent interests between stocks and bonds. " * 20):
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: MagicMock(
            status_code=200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.7" + b"x" * 20000))
        monkeypatch.setattr(fc, "_pdf_text", lambda content: text)
        article = {"id": "p-1", "source_id": "de-shaw", "title": "Divergent Interests", "url": url}
        return fc.content_fetcher_for(article)(article)

    def test_a_pdf_url_is_read_as_a_pdf(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch,
                        "https://www.deshaw.com/assets/articles/DESCO_Market_Insights_20220622.pdf")
        assert out[1] == "ok" and "Divergent interests" in out[0].read_text()

    def test_a_query_string_after_pdf_still_counts(self, tmp_path, monkeypatch):
        out = self._run(tmp_path, monkeypatch, "https://www.oaktreecapital.com/docs/press/x.pdf?sfvrsn=2")
        assert out[1] == "ok"

    def test_an_html_url_uses_the_source_fetcher(self):
        article = {"id": "h", "source_id": "de-shaw", "url": "https://www.deshaw.com/library/divergent-interests"}
        assert fc.content_fetcher_for(article) is fc.CONTENT_FETCHERS["de-shaw"]

    def test_main_uses_content_fetcher_for(self):
        assert "content_fetcher_for(" in inspect.getsource(fc.main)


def test_gsam_api_summary_is_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    resp = MagicMock(status_code=200, text="<html><body><main><span class='footer-disclosure-text'><p>Legal.</p></span></main></body></html>")
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
    out = fc._fetch_content_gsam({"id": "g-1", "url": "https://am.gs.com/x",
                                  "gsam_summary": "The private equity co-investment market is expanding. " * 4})
    assert out[1] == "metadata_only"
