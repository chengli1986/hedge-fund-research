"""Orphan bodies in content/ reach a human -- once (audit C2, 2026-10-01).

45 files with no row in data/articles.jsonl built up over five months and
nothing counted them. Tracing them found the cause was operations that removed
or re-id'd rows without moving the files (the 2026-09-14 lazard / cohen-steers
dedup merge left 17). The backlog was archived first, so from here on the
daily email reports only orphans that are new since its previous run: a
standing total repeated every morning is the alarm nobody reads.
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gmia-fetcher-health.py"
spec = importlib.util.spec_from_file_location("gfh_orphans", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_orphans"] = gfh
spec.loader.exec_module(gfh)

ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}


def _store(tmp_path, ids):
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps({"id": i}) + "\n" for i in ids))
    content = tmp_path / "content"
    content.mkdir()
    return data, content


class TestFindingThem:
    def test_a_body_with_no_row_is_an_orphan(self, tmp_path):
        data, content = _store(tmp_path, ["a1"])
        (content / "a1.txt").write_text("kept")
        (content / "b2.txt").write_text("row gone")
        (content / "notes.md").write_text("not a body")
        assert gfh.content_orphans(content, data) == ["b2"]

    def test_an_unreadable_store_is_unknown_not_all_orphans(self, tmp_path):
        content = tmp_path / "content"
        content.mkdir()
        (content / "a1.txt").write_text("x")
        # jsonl_store reads a missing file as an empty store; taken at face
        # value, every body would be an orphan.
        assert gfh.content_orphans(content, tmp_path / "missing" / "articles.jsonl") is None
        (tmp_path / "dir.jsonl").mkdir()
        assert gfh.content_orphans(content, tmp_path / "dir.jsonl") is None


class TestOnlyNewOnesAreReported:
    def test_only_the_new_one_is_reported_and_both_are_stored(self):
        new, stored = gfh.new_orphans(["b2", "c3"], {"orphans": ["b2"]})
        assert new == ["c3"] and stored == ["b2", "c3"]

    def test_first_run_with_no_stored_list_reports_what_it_finds(self):
        assert gfh.new_orphans(["b2"], {}) == (["b2"], ["b2"])

    def test_a_cleaned_up_orphan_drops_out_of_the_stored_list(self):
        assert gfh.new_orphans([], {"orphans": ["b2"]}) == ([], [])

    def test_an_unknown_run_keeps_the_stored_list(self):
        """Resetting it would report every standing orphan as new next time."""
        assert gfh.new_orphans(None, {"orphans": ["b2"]}) == ([], ["b2"])

    def test_a_malformed_stored_list_is_treated_as_empty(self):
        assert gfh.new_orphans(["b2"], {"orphans": "b2"}) == (["b2"], ["b2"])


class TestItReachesAHuman:
    ORPHANS = {"new": [{"id": "b2c3d4e5f6a7b8c9", "bytes": 2888, "written": "2026-10-02 03:49 BJT"}],
               "total": 1}

    def test_it_is_a_send_condition_and_names_itself(self):
        assert gfh.should_email(ALERTS, [], orphans=self.ORPHANS) is True
        assert "orphan" in gfh.alerts_subject(ALERTS, orphans=self.ORPHANS)
        html = gfh.render_html_email({}, ALERTS, {"sources": {}}, 1.0, orphans=self.ORPHANS)
        assert "b2c3d4e5f6a7b8c9" in html and "2888" in html and "C2" in html

    def test_a_mass_event_is_one_alarm_not_a_wall(self):
        many = {"new": [{"id": f"id{i:014d}", "bytes": 1, "written": "?"} for i in range(500)],
                "total": 500}
        html = gfh.render_html_email({}, ALERTS, {"sources": {}}, 1.0, orphans=many)
        assert html.count("id0000000000") <= gfh.ORPHAN_ROWS_SHOWN
        assert "480 more" in html

    def test_nothing_new_is_not_a_send_condition(self):
        assert gfh.should_email(ALERTS, [], orphans=None) is False
        html = gfh.render_html_email({}, ALERTS, {"sources": {}}, 1.0, orphans=None)
        assert "ORPHAN" not in html


def _quiet_main(monkeypatch, tmp_path, current, prev_state):
    seen, saved = {}, {}
    (tmp_path / "content").mkdir(exist_ok=True)
    for i in current:
        (tmp_path / "content" / f"{i}.txt").write_text("x" * 10)
    monkeypatch.setattr(gfh, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(gfh, "content_orphans", lambda *a, **k: list(current))
    monkeypatch.setattr(gfh, "load_sources", lambda: [])
    monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
    monkeypatch.setattr(gfh, "pipeline_intake_anomalies", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "pipeline_zero_fetches", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "recent_analysis_declines", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "entrypoint_problems", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "corrupt_state_backups", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "save_state", lambda st: saved.update(st))
    monkeypatch.setattr(gfh, "load_state", lambda: dict(prev_state))
    monkeypatch.setattr(gfh, "should_email", lambda *a, **k: seen.update(k) or False)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py"])
    gfh.main()
    return seen, saved


def test_main_reports_a_new_orphan_and_remembers_it(monkeypatch, tmp_path):
    """A detector nothing calls is not a signal -- check the wiring, not the parts."""
    seen, saved = _quiet_main(monkeypatch, tmp_path, ["b2"], {})
    assert seen.get("orphans") and seen["orphans"]["new"][0]["id"] == "b2"
    assert seen["orphans"]["new"][0]["bytes"] == 10
    assert saved.get("orphans") == ["b2"], "the state file must remember it for tomorrow"


def test_main_does_not_report_it_again_the_next_day(monkeypatch, tmp_path):
    seen, saved = _quiet_main(monkeypatch, tmp_path, ["b2"], {"orphans": ["b2"]})
    assert seen.get("orphans") is None
    assert saved.get("orphans") == ["b2"]
