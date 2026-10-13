"""Fixes from the 2026-10-12 full code review (106 batches, 56 findings).

One test per behaviour that changed; findings that did not hold up are listed
with the reason in the commit message, not here.
"""
import importlib.util
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import fetch_articles as fa
import fetch_content as fc
import publish

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load("refine_dates_cr", "scripts/refine_dates.py")
gfh = _load("gfh_cr", "scripts/gmia-fetcher-health.py")
vpp = _load("vpp_cr", "scripts/validate_pending_profile.py")


# ── fetchers ─────────────────────────────────────────────────────────────────

def test_de_shaw_reads_a_percent_encoded_share_link(monkeypatch):
    html = ('<article class="library-item"><span class="LibraryItem_year__x">2026</span>'
            '<a href="mailto:?body=Market%20Update%20-%20https%3A%2F%2Fwww.deshaw.com%2Flibrary%2Fmarket-update">'
            'share</a></article>')
    monkeypatch.setattr(fa, "_get_playwright_page", lambda *a, **k: html)
    out = fa.fetch_de_shaw({"id": "de-shaw", "url": "https://www.deshaw.com/library",
                            "expected_hostname": "deshaw.com", "max_articles": 10})
    assert [(a["title"], a["url"]) for a in out] == [
        ("Market Update", "https://www.deshaw.com/library/market-update")]


def test_a_solr_field_may_be_a_list_or_a_string():
    assert fa._first_or_self(["Week in Review", "x"]) == "Week in Review"
    assert fa._first_or_self("Fixed Income") == "Fixed Income"      # not "F"
    assert fa._first_or_self(None) == "" and fa._first_or_self([]) == ""


def test_bridgewater_keeps_its_paragraph_breaks():
    para = "Markets moved on the policy news and the dollar fell. " * 6
    # The live layout: one .RichTextBody block per section.
    html = (f"<html><body><div class='RichTextBody'>{para}</div>"
            f"<div class='RichTextBody'>{para}</div></body></html>")
    text = fc._extract_bridgewater_text(html)
    assert text and "\n" in text, "paragraphs were collapsed into one line"


def test_a_gsam_extraction_bug_is_not_logged_as_a_network_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    resp = MagicMock(status_code=200, text="<html><body><main><p>Body text.</p></main></body></html>")
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

    def broken(_p):
        raise AttributeError("helper changed")
    monkeypatch.setattr(fc, "_paragraph_text", broken)
    with caplog.at_level(logging.ERROR):
        fc._fetch_content_gsam({"id": "g-1", "url": "https://am.gs.com/x", "gsam_summary": ""})
    messages = [r.getMessage() for r in caplog.records]
    assert any("extraction failed (AttributeError)" in m for m in messages), messages
    assert not any("fetch failed" in m for m in messages)


# ── refine_dates ─────────────────────────────────────────────────────────────

def test_a_row_without_a_url_is_not_fetched_or_counted_as_an_error():
    def get(url):
        raise AssertionError(f"fetched {url!r}")
    new = rd.refine({"id": "x", "date": "2026-09-30", "url": None, "fetched_at": "2026-10-02T03:00:00+08:00"},
                    "2026-10-13", get=get)
    assert "date_fetch_errors" not in new and new.get("date_basis")


def test_flush_counts_dated_rows_not_retry_bookkeeping(tmp_path):
    path = tmp_path / "articles.jsonl"
    path.write_text("".join(json.dumps({"id": i, "date": "2026-09-30"}) + "\n" for i in ("a", "b")))
    results = {"a": {"date": "2026-09-12", "date_listed": "2026-09-30", "date_basis": "page"},
               "b": {"date_fetch_errors": 1, "_error": "Timeout"}}
    assert rd.flush(path, results) == 1


# ── fetcher-health ───────────────────────────────────────────────────────────

def test_the_newest_date_is_compared_as_a_date_not_a_string():
    assert max(["2026/09/30", "2026-10-12"], key=gfh._date_sort_key) == "2026-10-12"
    assert max(["junk", "2026-10-01"], key=gfh._date_sort_key) == "2026-10-01"


def test_failure_reasons_are_escaped_in_the_email():
    reason = "HTTP 403: <title>Access Denied</title> ?a=1&b=2"
    alerts = {"failing": [("aqr", {"reason": reason}, "OK")], "warning": [],
              "recovered": [], "healthy": []}
    html = gfh.render_html_email({}, alerts, {"sources": {}}, 1.0)
    assert "<title>Access Denied</title>" not in html
    assert "&lt;title&gt;Access Denied&lt;/title&gt;" in html


# ── auto-promote validator ───────────────────────────────────────────────────

def test_the_founded_ceiling_follows_the_calendar():
    assert vpp.FOUNDED_MAX == datetime.now(timezone.utc).year


def test_a_link_is_not_read_as_hedged_prose():
    data = {"id": "jpmam", "homepage_url": "https://am.jpmorgan.com/us/en/asset-management/adv/n/a/insights"}
    assert not any("high-risk" in i for i in vpp.validate_profile(data)["issues"])


# ── publish ──────────────────────────────────────────────────────────────────

def test_a_source_without_expected_hostname_is_escaped_once():
    view = publish._build_sources_view({"zz": {"name": "ZZ", "url": "https://zz.com/?a=1&b=2"}})
    assert "&amp;amp;" not in view and "a=1&amp;b=2" in view


def test_the_details_island_is_keyed_by_the_id_the_browser_sees():
    day = datetime.now().strftime("%Y-%m-%d")
    art = {"id": "x&y", "source_id": "man-group", "source_name": "Man", "title": "T",
           "url": "https://man.com/1", "date": day, "summarized": True,
           "summary_en": "s", "summary_zh": "s", "key_takeaway_en": "k", "key_takeaway_zh": "k"}
    html = publish.generate_html([art])
    island = html.split('id="article-details-data">', 1)[1].split("</script>", 1)[0]
    assert "a-x&y" in json.loads(island.replace("<\\/", "</"))
