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


class TestF1ChallengePageThroughPlaywright:
    """fetch_with_evidence records only requests.Session calls, so a fetcher that
    gets its HTML from Playwright (10 of 42 sources) never yields a response
    record and the classifier's challenge/403 rules cannot fire. A Cloudflare
    "Just a moment..." page went into _normalize_html, matched no selector,
    and came out as selector_miss -- "the site was redesigned, fix the
    selector" (daily x3 then permafail, code-dependent) instead of
    blocked_by_bot_protection (weekly x4). The page itself is the evidence,
    whatever transport delivered it."""

    CHALLENGE = ('<html><head><title>Just a moment...</title></head><body>'
                 '<div id="cf-browser-verification">Enable JavaScript and cookies to continue'
                 '</div></body></html>')

    def test_normalize_html_recognises_a_challenge_page(self):
        fc.drain_extraction_paths()
        fc._failure_hints.clear()
        text = fc._normalize_html(self.CHALLENGE, "article p")
        assert text == ""
        assert "challenge" in fc.drain_extraction_paths()
        assert any(label == "blocked_by_bot_protection" for label, _ in fc._failure_hints)

    def test_a_playwright_shaped_failure_is_labelled_blocked_not_selector_miss(self):
        import failure_labels as fl

        def playwright_fetcher(article):
            # no requests.Session call: the HTML "came from a browser"
            body = fc._normalize_html(self.CHALLENGE, "article p")
            if not fc._check_min_content_length(body):
                fc.log.warning("  X: extracted text too short (%d chars)", len(body))
                return None

        _, evidence = fc.fetch_with_evidence({"id": "x", "source_id": "gmo", "url": "https://x/a"},
                                             playwright_fetcher)
        assert evidence["responses"] == []
        label, _ = fl.classify_content_failure(evidence)
        assert label == "blocked_by_bot_protection"

    def test_an_ordinary_page_is_not_a_challenge(self):
        fc.drain_extraction_paths()
        fc._failure_hints.clear()
        fc._normalize_html("<html><body><article><p>" + "real text " * 30 + "</p></article></body></html>", "article p")
        assert fc.drain_extraction_paths() == ["primary"]
        assert not fc._failure_hints


def _resp(status, url, ctype="text/html", links=50, challenge=False):
    return {"status": status, "url": url, "final_url": url, "content_type": ctype,
            "challenge": challenge, "links": links, "media_player": False}


def _classify(**ev):
    import failure_labels as fl
    return fl.classify_content_failure({"exception": None, "hints": [], "extraction_paths": [],
                                        "responses": [], "messages": [], **ev})


class TestF2TheFailingResponseIsTheEvidence:
    """GMO and Oaktree fetch the article page (200) and then its PDF; the
    classifier judged responses[0], so a 404 on the PDF was body_too_short
    (daily x3, code-dependent, nobody emailed) instead of page_gone."""

    def test_a_pdf_404_after_a_200_page_is_page_gone(self):
        label, detail = _classify(
            responses=[_resp(200, "https://gmo/x"), _resp(404, "https://gmo/x.pdf", links=3)],
            messages=["  GMO: invalid PDF response (status=404, type=text/html, size=1200)"])
        assert label == "page_gone" and "x.pdf" in detail

    def test_a_pdf_403_after_a_200_page_is_blocked(self):
        label, _ = _classify(
            responses=[_resp(200, "https://gmo/x"), _resp(403, "https://gmo/x.pdf", links=0)])
        assert label == "blocked_by_bot_protection"

    def test_a_pdf_500_after_a_200_page_is_fetch_error(self):
        label, _ = _classify(
            responses=[_resp(200, "https://gmo/x"), _resp(500, "https://gmo/x.pdf", links=0)])
        assert label == "fetch_error"

    def test_a_single_200_page_still_judges_that_page(self):
        label, _ = _classify(responses=[_resp(200, "https://x/a")], messages=["  X: too short"])
        assert label == "body_too_short"


class TestF3EveryCapturedMessageIsEvidence:
    """Only messages[-1] was matched against _ERROR_MESSAGE, and eleven of the
    fetchers' failure messages matched nothing. T. Rowe Price logs two
    Playwright timeouts and then "all attempts failed, giving up" -- the last
    line hid the two informative ones and the night was body_too_short."""

    def test_a_double_playwright_timeout_is_fetch_error(self):
        label, _ = _classify(messages=[
            "  T.Rowe Price: Playwright attempt 1 failed: Page.goto: Timeout 30000ms exceeded.",
            "  T.Rowe Price: Playwright attempt 2 failed: Page.goto: Timeout 30000ms exceeded.",
            "  T.Rowe Price: all attempts failed, giving up"])
        assert label == "fetch_error"

    def test_a_failed_pdf_download_is_fetch_error(self):
        label, _ = _classify(messages=["  Oaktree: failed to download PDF: HTTPSConnectionPool(...): Read timed out."])
        assert label == "fetch_error"

    def test_a_pdf_that_will_not_parse_is_pdf_not_usable(self):
        label, _ = _classify(
            responses=[_resp(200, "https://gmo/x"), _resp(200, "https://gmo/x.pdf", ctype="application/pdf")],
            messages=["  GMO: pdfplumber extraction failed: No /Root object! - Is this really a PDF?"])
        assert label == "pdf_not_usable"

    def test_a_pdf_served_as_html_is_pdf_not_usable(self):
        label, _ = _classify(
            responses=[_resp(200, "https://gmo/x"), _resp(200, "https://gmo/x.pdf", links=40)],
            messages=["  GMO: invalid PDF response (status=200, type=text/html, size=48211)"])
        assert label == "pdf_not_usable"

    def test_a_linked_pdf_extraction_failure_is_pdf_not_usable(self):
        label, _ = _classify(messages=["  MetLife: PDF extraction failed https://m/x.pdf: EOF marker not found"])
        assert label == "pdf_not_usable"
