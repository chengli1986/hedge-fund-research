"""The A/B gate: a source moves to a template only on evidence (2026-09-21)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("compare_fetchers", REPO / "scripts" / "compare_fetchers.py")
cf = importlib.util.module_from_spec(spec)
sys.modules["compare_fetchers"] = cf
spec.loader.exec_module(cf)

import fetch_articles as fa
import listing_templates as lt

SRC = {"id": "t", "name": "T", "short_name": "T", "method": "ssr", "url": "https://site.test/i",
       "expected_hostname": "site.test", "max_articles": 10,
       "listing_template": {"type": "card_list", "fetch": "requests", "card": "div.card",
                            "link": "a", "date": "span.date"}}
ROWS = [{"title": "A", "url": "https://site.test/a", "date": "2026-09-01", "date_raw": "Sep 1, 2026"},
        {"title": "B", "url": "https://site.test/b", "date": "2026-08-01", "date_raw": "Aug 1, 2026"}]


def _patch(monkeypatch, bespoke, template):
    monkeypatch.setitem(fa.FETCHERS, "t", lambda s: [dict(r) for r in bespoke])
    monkeypatch.setattr(lt, "fetch", lambda s: [dict(r) for r in template])
    monkeypatch.setattr(fa, "load_entrypoints", lambda: {})


def test_identical_output_agrees(monkeypatch):
    _patch(monkeypatch, ROWS, ROWS)
    assert cf.compare_source(dict(SRC))["agree"] is True


def test_a_missing_article_does_not_agree(monkeypatch):
    _patch(monkeypatch, ROWS, ROWS[:1])
    out = cf.compare_source(dict(SRC))
    assert out["agree"] is False and len(out["only_bespoke"]) == 1


def test_an_extra_article_does_not_agree(monkeypatch):
    _patch(monkeypatch, ROWS[:1], ROWS)
    out = cf.compare_source(dict(SRC))
    assert out["agree"] is False and len(out["only_template"]) == 1


def test_a_different_date_does_not_agree(monkeypatch):
    changed = [dict(ROWS[0], date="2026-09-02"), ROWS[1]]
    _patch(monkeypatch, ROWS, changed)
    assert cf.compare_source(dict(SRC))["agree"] is False


def test_the_same_articles_in_a_different_order_is_reported(monkeypatch):
    _patch(monkeypatch, ROWS, list(reversed(ROWS)))
    out = cf.compare_source(dict(SRC))
    assert out["agree"] is False and out["order_differs"] is True


def test_a_raising_fetcher_is_an_error_not_a_pass(monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "t", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(lt, "fetch", lambda s: ROWS)
    monkeypatch.setattr(fa, "load_entrypoints", lambda: {})
    out = cf.compare_source(dict(SRC))
    assert out["agree"] is False and "boom" in out["error"]


def test_date_raw_is_not_compared(monkeypatch):
    """It is descriptive; the stored fields are title, url and date."""
    other_raw = [dict(r, date_raw="whatever") for r in ROWS]
    _patch(monkeypatch, ROWS, other_raw)
    assert cf.compare_source(dict(SRC))["agree"] is True


class TestDroppedListingFields:
    """(title, url, date) agreement hides fields the template does not emit.

    fetch_articles stores LISTING_FIELDS_KEPT ("summary", "category",
    "gsam_summary") on every row that carries them.  A hand-written fetcher
    that fills one of those and a template that does not compare as identical,
    so the field would quietly stop being stored from the switch onward -- and
    gsam_summary is read by fetch_content.  A drop has to be declared in
    sources.json ("listing_template_drops") to pass.
    """

    def test_an_undeclared_dropped_field_is_not_agreement(self, monkeypatch):
        bespoke = [dict(r, category="Markets") for r in ROWS]
        _patch(monkeypatch, bespoke, ROWS)
        out = cf.compare_source(dict(SRC))
        assert out["agree"] is False
        assert out["dropped_fields"] == ["category"]

    def test_a_declared_drop_agrees_and_is_still_reported(self, monkeypatch):
        bespoke = [dict(r, category="Markets") for r in ROWS]
        _patch(monkeypatch, bespoke, ROWS)
        out = cf.compare_source(dict(SRC, listing_template_drops=["category"]))
        assert out["agree"] is True
        assert out["dropped_fields"] == ["category"]

    def test_a_field_the_template_also_emits_is_not_a_drop(self, monkeypatch):
        rows = [dict(r, category="Markets") for r in ROWS]
        _patch(monkeypatch, rows, rows)
        out = cf.compare_source(dict(SRC))
        assert out["agree"] is True and out["dropped_fields"] == []

    def test_fields_outside_what_gets_stored_are_ignored(self, monkeypatch):
        """content_type is a fetcher-local field; it never reaches the store."""
        bespoke = [dict(r, content_type="Read") for r in ROWS]
        _patch(monkeypatch, bespoke, ROWS)
        assert cf.compare_source(dict(SRC))["agree"] is True
