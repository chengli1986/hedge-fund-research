"""`--dry-run` must not write production state (2026-09-16).

`fetch_articles.py --source X --dry-run` is the tool for looking at a source
without touching anything -- it is what every source investigation in this
repo starts with. It skipped save_articles but still called
record_quality_metrics, so each probe rewrote config/inspection_state.json:
last_inspected_at moved, and consecutive_zero_count -- the counter the
fetcher-health email uses to decide a source has gone silent -- was advanced
or reset by a dry run. Probing a blocked source twice could clear a real zero
streak; probing a healthy one in dry-run mode could start one.
"""
import json

import pytest

import fetch_articles as fa

SRC = {"id": "gsam", "name": "GSAM", "short_name": "GSAM", "method": "api",
       "url": "https://x/", "expected_hostname": ""}
LISTED = [{"title": "T", "url": "https://am.gs.com/y", "date": "2026-09-14"}]


@pytest.fixture
def state(tmp_path, monkeypatch):
    f = tmp_path / "inspection_state.json"
    monkeypatch.setattr(fa, "INSPECTION_STATE_FILE", f)
    return f


def test_a_dry_run_leaves_inspection_state_untouched(state, monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: LISTED)
    fa.fetch_source(SRC, set(), dry_run=True)
    assert not state.exists(), "a dry run wrote production state"


def test_a_real_run_still_records_the_metrics(state, monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: LISTED)
    fa.fetch_source(SRC, set())
    assert json.loads(state.read_text())["gsam"]["last_article_count"] == 1


def test_a_dry_run_of_a_raising_fetcher_writes_nothing(state, monkeypatch):
    def boom(s):
        raise RuntimeError("blocked")
    monkeypatch.setitem(fa.FETCHERS, "gsam", boom)
    assert fa.fetch_source(SRC, set(), dry_run=True) == []
    assert not state.exists(), "the failure path wrote production state in a dry run"


def test_a_real_run_of_a_raising_fetcher_still_records_the_zero(state, monkeypatch):
    """That zero is what the health email reads; it must survive the fix."""
    def boom(s):
        raise RuntimeError("blocked")
    monkeypatch.setitem(fa.FETCHERS, "gsam", boom)
    fa.fetch_source(SRC, set())
    assert json.loads(state.read_text())["gsam"]["consecutive_zero_count"] == 1


def test_a_dry_run_of_a_broken_sort_order_writes_nothing(state, monkeypatch):
    out_of_order = [{"title": "A", "url": "https://am.gs.com/a", "date": "2026-01-01"},
                    {"title": "B", "url": "https://am.gs.com/b", "date": "2026-09-01"}]
    monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: out_of_order)
    assert fa.fetch_source(dict(SRC, date_sorted=True), set(), dry_run=True) == []
    assert not state.exists(), "the date-order refusal wrote production state in a dry run"
