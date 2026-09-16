"""The intake anomalies that had no destination but log.warning (2026-09-16).

fetch_articles.check_anomalies produces four signals per source. One (two
consecutive zero-article fetches) reaches the daily email through
pipeline_zero_fetches. The other three did not reach anybody -- the same
"detected but never communicated" shape this repo has now hit six times.

Reading the 42 configured sources' real records first changed what was worth
building:

  last_gated_ratio        0.0 everywhere; only fetch_gmo sets `gated` at all
                          (its API exposes a `Lock` field), so for the other
                          41 sources it is structurally zero, not measured.
  last_valid_body_ratio   1.0 everywhere -- because it IS 1 - gated_ratio.
                          Its alert text ("content extraction failing")
                          describes something it does not measure: extraction
                          happens in stage 2 and is not in this record. It
                          fires only when gated_ratio > 0.7, a strict subset
                          of the gated alert. A duplicate signal wearing a
                          wrong explanation is worse than no signal, so it is
                          gone rather than plumbed.
  last_mismatch_count     0 everywhere, and it can fire for any source: it
                          counts listing URLs that are not on the declared
                          host -- fetcher drift or a half-done config edit.

So two real signals reach the email, at thresholds the real data supports.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gmia-fetcher-health.py"
spec = importlib.util.spec_from_file_location("gfh_intake", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_intake"] = gfh
spec.loader.exec_module(gfh)

import fetch_articles as fa


def _fresh(hours=0.75):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _state(tmp_path, payload):
    f = tmp_path / "inspection_state.json"
    f.write_text(json.dumps(payload))
    return f


def _rec(**kw):
    base = {"last_inspected_at": _fresh(), "last_article_count": 10,
            "last_gated_ratio": 0.0, "last_mismatch_count": 0}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "gmo"}, {"id": "aqr"}, {"id": "acadian-asset"}])


class TestDetection:
    def test_urls_off_the_declared_host_are_reported(self, tmp_path):
        f = _state(tmp_path, {"aqr": _rec(last_mismatch_count=2)})
        out = gfh.pipeline_intake_anomalies(state_path=f)
        assert [sid for sid, _ in out] == ["aqr"]
        assert "host" in out[0][1][0].lower() and "2" in out[0][1][0]

    def test_one_stray_url_is_enough(self, tmp_path):
        """All 42 sources sit at 0, so one is already an outlier -- and the
        old >3 threshold meant three strays a night were invisible forever."""
        f = _state(tmp_path, {"aqr": _rec(last_mismatch_count=1)})
        assert [sid for sid, _ in gfh.pipeline_intake_anomalies(state_path=f)] == ["aqr"]

    def test_a_mostly_locked_listing_is_reported(self, tmp_path):
        f = _state(tmp_path, {"gmo": _rec(last_gated_ratio=0.6)})
        out = gfh.pipeline_intake_anomalies(state_path=f)
        assert [sid for sid, _ in out] == ["gmo"]
        assert "60%" in out[0][1][0]

    def test_a_normal_record_is_silent(self, tmp_path):
        f = _state(tmp_path, {"aqr": _rec(), "gmo": _rec(last_gated_ratio=0.2)})
        assert gfh.pipeline_intake_anomalies(state_path=f) == []

    def test_a_source_no_longer_configured_is_ignored(self, tmp_path):
        f = _state(tmp_path, {"pgim": _rec(last_mismatch_count=9)})
        assert gfh.pipeline_intake_anomalies(state_path=f) == []

    def test_a_stale_record_is_ignored(self, tmp_path):
        """Same reason as zero-fetch reporting: a frozen record would alert
        every day with no way to clear it."""
        f = _state(tmp_path, {"aqr": _rec(last_inspected_at=_fresh(hours=72), last_mismatch_count=5)})
        assert gfh.pipeline_intake_anomalies(state_path=f) == []

    def test_a_broken_state_file_is_silent_not_fatal(self, tmp_path):
        f = tmp_path / "inspection_state.json"
        f.write_text("{not json")
        assert gfh.pipeline_intake_anomalies(state_path=f) == []

    def test_both_signals_on_one_source_are_both_listed(self, tmp_path):
        f = _state(tmp_path, {"gmo": _rec(last_gated_ratio=0.9, last_mismatch_count=4)})
        out = gfh.pipeline_intake_anomalies(state_path=f)
        assert len(out) == 1 and len(out[0][1]) == 2


class TestItReachesAHuman:
    ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}

    def test_it_is_a_send_condition_on_its_own(self):
        anomalies = [("aqr", ["3 listing URLs were not on the declared host"])]
        assert gfh.should_email(self.ALERTS, [], intake=anomalies) is True
        assert gfh.should_email(self.ALERTS, []) is False

    def test_it_is_in_the_email_body(self):
        anomalies = [("aqr", ["3 listing URLs were not on the declared host"])]
        html = gfh.render_html_email({}, self.ALERTS, {"sources": {}}, 1.0, intake=anomalies)
        assert "aqr" in html and "not on the declared host" in html

    def test_it_is_in_the_subject(self):
        anomalies = [("aqr", ["3 listing URLs were not on the declared host"])]
        subject = gfh.alerts_subject(self.ALERTS, intake=anomalies)
        assert "aqr" in subject


class TestTheDuplicateSignalIsGone:
    def test_check_anomalies_no_longer_claims_extraction_is_failing(self):
        """It measured 1 - gated_ratio and called it content extraction."""
        alerts = fa.check_anomalies({"last_valid_body_ratio": 0.1, "last_gated_ratio": 0.0,
                                     "last_mismatch_count": 0, "consecutive_zero_count": 0})
        assert alerts == []

    def test_the_gated_signal_still_fires(self):
        alerts = fa.check_anomalies({"last_gated_ratio": 0.9, "last_mismatch_count": 0,
                                     "consecutive_zero_count": 0})
        assert len(alerts) == 1 and "locked" in alerts[0].lower()

    def test_the_field_is_no_longer_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa, "INSPECTION_STATE_FILE", tmp_path / "s.json")
        fa.record_quality_metrics("aqr", 10, 2, 0, 0)
        rec = json.loads((tmp_path / "s.json").read_text())["aqr"]
        assert "last_valid_body_ratio" not in rec
        assert rec["last_gated_ratio"] == 0.0 and rec["last_mismatch_count"] == 0


def test_main_computes_it_and_passes_it_on(monkeypatch, tmp_path, capsys):
    """A detector nothing calls is not a signal -- the failure this whole
    family of bugs is made of."""
    seen = {}
    monkeypatch.setattr(gfh, "load_sources", lambda: [])
    monkeypatch.setattr(gfh, "pipeline_intake_anomalies",
                        lambda *a, **k: [("aqr", ["3 listing URLs were not on the declared host"])])
    monkeypatch.setattr(gfh, "pipeline_zero_fetches", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "recent_analysis_declines", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "load_state", lambda: {})
    monkeypatch.setattr(gfh, "should_email", lambda *a, **k: seen.update(k) or False)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py"])
    gfh.main()
    assert seen.get("intake"), "main did not pass intake anomalies to the send decision"
    assert "INTAKE ANOMALIES" in capsys.readouterr().out


class TestDamagedStoreReachesAHuman:
    """jsonl_store reports a damaged row with log.error, and audit B8 exists
    because a log line is not a destination. The daily email carries it too.
    """
    def _store(self, tmp_path, text):
        f = tmp_path / "articles.jsonl"
        f.write_text(text)
        return f

    def test_a_damaged_row_is_counted(self, tmp_path):
        f = self._store(tmp_path, '{"id": "a1"}\ntorn line\n{"id": "a3"}\n')
        assert gfh.store_damage(f) == 1

    def test_a_clean_store_reports_nothing(self, tmp_path):
        f = self._store(tmp_path, '{"id": "a1"}\n{"id": "a2"}\n')
        assert gfh.store_damage(f) == 0

    def test_a_missing_store_is_not_damage(self, tmp_path):
        assert gfh.store_damage(tmp_path / "nope.jsonl") == 0

    def test_it_is_a_send_condition_and_names_itself(self):
        alerts = {"failing": [], "warning": [], "recovered": [], "healthy": []}
        assert gfh.should_email(alerts, [], damaged_rows=2) is True
        assert "2" in gfh.alerts_subject(alerts, damaged_rows=2)
        html = gfh.render_html_email({}, alerts, {"sources": {}}, 1.0, damaged_rows=2)
        assert "articles.jsonl" in html and "2" in html

    def test_main_computes_it(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(gfh, "load_sources", lambda: [])
        monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 3)
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
        assert seen.get("damaged_rows") == 3
