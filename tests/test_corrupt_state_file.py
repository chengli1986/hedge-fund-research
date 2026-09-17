"""An unreadable state file must not erase the fleet's alarm clock (audit B4).

config/inspection_state.json holds, per source, last_article_count and
consecutive_zero_count -- the counter the fetcher-health email reads to decide
a source has gone silent. record_quality_metrics read it, and on a
json.JSONDecodeError fell back to `state = {}` and then wrote its own source
back as the WHOLE file. One unreadable read therefore deleted every other
source's history: 42 sources' zero streaks reset to 0, so a source that had
been silent for days started counting from scratch and its alert was pushed
back by as many nights.

The file is written with a temp file and os.replace, so it is not usually
half-written -- but it is written 42 times a night, it is not fsynced, and a
host crash is exactly the input that produces an unreadable one.

The fix cannot be "refuse to write": the fleet would then record nothing until
someone noticed. It is to keep the evidence (a timestamped copy of the bad
file, which also allows the counters to be restored by hand), say so loudly,
and let the run continue -- and to let the daily health email see that it
happened, because a log line is not a destination.
"""
import json
import logging

import pytest

import fetch_articles as fa


@pytest.fixture
def state(tmp_path, monkeypatch):
    f = tmp_path / "inspection_state.json"
    monkeypatch.setattr(fa, "INSPECTION_STATE_FILE", f)
    return f


def test_a_valid_file_keeps_every_other_source(state):
    state.write_text(json.dumps({"aqr": {"consecutive_zero_count": 3, "last_article_count": 0}}))
    fa.record_quality_metrics("gmo", 5, 1, 0, 0)
    after = json.loads(state.read_text())
    assert after["aqr"]["consecutive_zero_count"] == 3
    assert after["gmo"]["last_article_count"] == 5


def test_an_unreadable_file_is_preserved_before_it_is_replaced(state, caplog):
    state.write_text('{"aqr": {"consecutive_zero_count": 3}, "gmo": {"cons')   # torn
    with caplog.at_level(logging.ERROR):
        fa.record_quality_metrics("gmo", 5, 1, 0, 0)
    backups = list(state.parent.glob("inspection_state.json.corrupt-*"))
    assert len(backups) == 1, "the counters were thrown away with no way to get them back"
    assert "cons" in backups[0].read_text()
    assert any("STATE FILE UNREADABLE" in r.getMessage() for r in caplog.records)


def test_the_run_still_records_after_a_corrupt_read(state):
    state.write_text('{"aqr": {"consecutive_zero_count": 3}, "gmo": {"cons')
    fa.record_quality_metrics("gmo", 0, 0, 0, 0)
    after = json.loads(state.read_text())
    assert after["gmo"]["consecutive_zero_count"] == 1
    assert after["gmo"]["last_article_count"] == 0


def test_a_second_source_in_the_same_run_does_not_back_up_again(state):
    state.write_text('{"aqr": {"consecutive_zero_count": 3}, "gmo": {"cons')
    fa.record_quality_metrics("gmo", 5, 1, 0, 0)
    fa.record_quality_metrics("aqr", 5, 1, 0, 0)          # file is valid again
    assert len(list(state.parent.glob("inspection_state.json.corrupt-*"))) == 1


def test_a_dry_run_neither_writes_nor_backs_up(state):
    state.write_text('{"aqr": {"consecutive_zero_count": 3}, "gmo": {"cons')
    before = state.read_text()
    fa.record_quality_metrics("gmo", 5, 1, 0, 0, dry_run=True)
    assert state.read_text() == before
    assert not list(state.parent.glob("inspection_state.json.corrupt-*"))


def test_a_file_holding_something_that_is_not_an_object_is_also_corrupt(state):
    state.write_text('["aqr", "gmo"]')
    fa.record_quality_metrics("gmo", 5, 1, 0, 0)
    assert len(list(state.parent.glob("inspection_state.json.corrupt-*"))) == 1
    assert json.loads(state.read_text())["gmo"]["last_article_count"] == 5


class TestItReachesAHuman:
    def test_the_health_check_reports_the_backups(self, tmp_path, monkeypatch):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "gfh_state", fa.BASE_DIR / "scripts" / "gmia-fetcher-health.py")
        gfh = importlib.util.module_from_spec(spec)
        sys.modules["gfh_state"] = gfh
        spec.loader.exec_module(gfh)
        f = tmp_path / "inspection_state.json"
        (tmp_path / "inspection_state.json.corrupt-20260917-0345").write_text("{}")
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", f)
        found = gfh.corrupt_state_backups()
        assert len(found) == 1
        alerts = {"failing": [], "warning": [], "recovered": [], "healthy": []}
        assert gfh.should_email(alerts, [], corrupt_state=found) is True
        assert "state" in gfh.alerts_subject(alerts, corrupt_state=found).lower()
        html = gfh.render_html_email({}, alerts, {"sources": {}}, 1.0, corrupt_state=found)
        assert "corrupt-20260917-0345" in html

    def test_main_looks_for_them(self, monkeypatch):
        """A check nothing calls is not a check."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "gfh_state_main", fa.BASE_DIR / "scripts" / "gmia-fetcher-health.py")
        gfh = importlib.util.module_from_spec(spec)
        sys.modules["gfh_state_main"] = gfh
        spec.loader.exec_module(gfh)
        seen = {}
        monkeypatch.setattr(gfh, "load_sources", lambda: [])
        monkeypatch.setattr(gfh, "corrupt_state_backups", lambda *a, **k: ["inspection_state.json.corrupt-x"])
        monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
        monkeypatch.setattr(gfh, "pipeline_intake_anomalies", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "pipeline_zero_fetches", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "recent_analysis_declines", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
        monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
        monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
        monkeypatch.setattr(gfh, "load_state", lambda: {})
        monkeypatch.setattr(gfh, "should_email", lambda *a, **k: seen.update(k) or False)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py"])
        gfh.main()
        assert seen.get("corrupt_state") == ["inspection_state.json.corrupt-x"]
