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


def test_the_same_articles_in_a_different_order_is_reported_but_passes(monkeypatch):
    """Same set, different order: the ingestion outcome is identical.

    Ids are derived from the URL, fetch_source sorts the dates it guards on,
    and publish orders by date, so nothing downstream reads listing order.
    msci-research serves its cards in a varying order and would otherwise
    fail the weekly gate about half the time for nothing.
    """
    _patch(monkeypatch, ROWS, list(reversed(ROWS)))
    out = cf.compare_source(dict(SRC))
    assert out["agree"] is True and out["order_differs"] is True


def test_order_still_matters_for_a_date_sorted_source(monkeypatch):
    """date_sorted is load-bearing: fetch_source refuses every article when a
    source that declares it comes back out of order (DATE_ORDER_BROKEN)."""
    _patch(monkeypatch, ROWS, list(reversed(ROWS)))
    out = cf.compare_source(dict(SRC, date_sorted=True))
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


class TestRecheck:
    """A weekly cron needs the gate to fail only on real drift.

    Two live-fetch races are known and harmless: a site that publishes
    between the two sequential fetches (wellington, 2026-09-25) and a listing
    served in a varying card order (msci-research, which renders exactly
    max_articles cards so the set cannot change). Both clear on a re-run.
    --recheck re-runs only the sources that disagreed, once, and reports what
    it did -- a retry queue is only meaningful for flaky failures, so a
    source that disagrees twice still fails.
    """

    def _run(self, monkeypatch, sequence, argv):
        """sequence: list of agree-values compare_source returns in order."""
        calls = []

        def fake_compare(source):
            calls.append(source["id"])
            return {"source_id": source["id"], "agree": sequence.pop(0), "error": None,
                    "bespoke": 1, "template": 1, "only_bespoke": [], "only_template": [],
                    "order_differs": False, "dropped_fields": []}

        monkeypatch.setattr(cf, "compare_source", fake_compare)
        monkeypatch.setattr(cf.sys, "argv", ["compare_fetchers.py"] + argv + ["--no-record"])
        monkeypatch.setattr(cf, "_load_sources", lambda: [dict(SRC)])
        return cf.main(), calls

    def test_a_source_that_clears_on_the_second_try_passes(self, monkeypatch):
        code, calls = self._run(monkeypatch, [False, True], ["--all", "--recheck"])
        assert code == 0 and calls == ["t", "t"]

    def test_a_source_that_disagrees_twice_still_fails(self, monkeypatch):
        code, calls = self._run(monkeypatch, [False, False], ["--all", "--recheck"])
        assert code == 1 and calls == ["t", "t"]

    def test_an_agreeing_source_is_not_fetched_again(self, monkeypatch):
        code, calls = self._run(monkeypatch, [True], ["--all", "--recheck"])
        assert code == 0 and calls == ["t"]

    def test_without_the_flag_a_single_disagreement_fails(self, monkeypatch):
        code, calls = self._run(monkeypatch, [False], ["--all"])
        assert code == 1 and calls == ["t"]


class TestOutputLayout:
    """A note must sit under the source it belongs to.

    Written first at the wrong place: it printed before its own source's
    line, so in the weekly alert it read as a note about the source above.
    """

    def _print(self, monkeypatch, capsys, result):
        monkeypatch.setattr(cf, "compare_source", lambda s: result)
        monkeypatch.setattr(cf, "_load_sources", lambda: [dict(SRC)])
        monkeypatch.setattr(cf.sys, "argv", ["compare_fetchers.py", "--all", "--no-record"])
        cf.main()
        return capsys.readouterr().out.splitlines()

    def test_the_order_note_comes_after_its_own_source_line(self, monkeypatch, capsys):
        lines = self._print(monkeypatch, capsys, {
            "source_id": "t", "agree": True, "error": None, "bespoke": 2, "template": 2,
            "only_bespoke": [], "only_template": [], "order_differs": True,
            "dropped_fields": []})
        assert "t" in lines[0] and "different order" in lines[1]

    def test_the_dropped_fields_note_comes_after_its_own_source_line(self, monkeypatch, capsys):
        lines = self._print(monkeypatch, capsys, {
            "source_id": "t", "agree": True, "error": None, "bespoke": 2, "template": 2,
            "only_bespoke": [], "only_template": [], "order_differs": False,
            "dropped_fields": ["category"]})
        assert "t" in lines[0] and "template drops: category" in lines[1]


class TestSnapshot:
    """The weekly result has to outlive its stdout.

    The gate mails only on failure, so a green week left no trace at all and
    the health page had nothing to show. --all now appends a snapshot, which
    means the crontab line installed on 2026-09-26 keeps working unchanged.
    """

    def _run(self, monkeypatch, tmp_path, agree, argv=("--all",)):
        monkeypatch.setattr(cf, "HISTORY", tmp_path / "ab-gate.jsonl")
        monkeypatch.setattr(cf, "_load_sources", lambda: [dict(SRC), dict(SRC, id="u")])
        seq = list(agree)
        monkeypatch.setattr(cf, "compare_source", lambda s: {
            "source_id": s["id"], "agree": seq.pop(0), "error": None, "bespoke": 1,
            "template": 1, "only_bespoke": [], "only_template": [], "order_differs": False,
            "dropped_fields": []})
        monkeypatch.setattr(cf.sys, "argv", ["compare_fetchers.py", *argv])
        code = cf.main()
        rows = [json.loads(l) for l in (tmp_path / "ab-gate.jsonl").read_text().splitlines()] \
            if (tmp_path / "ab-gate.jsonl").exists() else []
        return code, rows

    def test_a_green_run_records_that_it_was_green(self, monkeypatch, tmp_path):
        code, rows = self._run(monkeypatch, tmp_path, [True, True])
        assert code == 0 and len(rows) == 1
        assert rows[0]["agree"] is True and rows[0]["disagreed"] == [] and rows[0]["sources"] == 2

    def test_a_disagreement_is_recorded_with_the_source_that_differed(self, monkeypatch, tmp_path):
        code, rows = self._run(monkeypatch, tmp_path, [True, False])
        assert code == 1 and rows[0]["agree"] is False and rows[0]["disagreed"] == ["u"]

    def test_no_record_writes_nothing(self, monkeypatch, tmp_path):
        code, rows = self._run(monkeypatch, tmp_path, [True, True], argv=("--all", "--no-record"))
        assert code == 0 and rows == []

    def test_a_single_source_run_records_nothing(self, monkeypatch, tmp_path):
        """One source says nothing about the fleet; only --all is a snapshot."""
        monkeypatch.setattr(cf, "HISTORY", tmp_path / "ab-gate.jsonl")
        monkeypatch.setattr(cf, "_load_sources", lambda: [dict(SRC)])
        monkeypatch.setattr(cf, "compare_source", lambda s: {
            "source_id": s["id"], "agree": True, "error": None, "bespoke": 1, "template": 1,
            "only_bespoke": [], "only_template": [], "order_differs": False, "dropped_fields": []})
        monkeypatch.setattr(cf.sys, "argv", ["compare_fetchers.py", "--source", "t"])
        cf.main()
        assert not (tmp_path / "ab-gate.jsonl").exists()
