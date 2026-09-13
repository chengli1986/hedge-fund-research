"""Articles stage 3 declined to summarise must reach a human.

Since e10d923 an article whose text is not an article (navigation, a
disclaimer, chart notes, a login wall) gets analysis_status
"insufficient_content" instead of an invented summary. That is only half the
fix: those declines cluster by source, and a cluster means stage 2 is saving
the wrong thing -- lazard-am had 27, oaktree saved a broker-dealer disclosure,
gmo an employee tax notice. Left in articles.jsonl and a stdout count in
gmia.log, that knowledge reaches nobody, which is the defect this repo keeps
reintroducing. So the 04:30 health email reports them, grouped by source,
with the model's own reason.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gfh_declines", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_declines"] = gfh
spec.loader.exec_module(gfh)

NO_ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}


def _hours_ago(n):
    # Relative, never a literal: the freshness window is wall-clock based.
    return (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=n)).isoformat(timespec="seconds")


def _data(tmp_path, rows):
    f = tmp_path / "articles.jsonl"
    f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return f


def _declined(sid, n_hours, reason="The text is a legal disclaimer.", i=0):
    return {"id": f"{sid}-{i}", "source_id": sid, "title": f"T{i}", "summarized": False,
            "analysis_status": "insufficient_content", "analysis_reason": reason,
            "analysis_checked_at": _hours_ago(n_hours)}


class TestRecentDeclines:
    def _run(self, tmp_path, monkeypatch, rows, configured=("lazard-am", "gmo", "aqr")):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": s} for s in configured])
        return gfh.recent_analysis_declines(_data(tmp_path, rows))

    def test_fresh_declines_are_grouped_by_source(self, tmp_path, monkeypatch):
        rows = [_declined("lazard-am", 3, i=0), _declined("lazard-am", 4, i=1),
                _declined("gmo", 2, reason="The text is a health coverage tax document.")]
        got = dict(self._run(tmp_path, monkeypatch, rows))
        assert set(got) == {"lazard-am", "gmo"}
        assert len(got["lazard-am"]) == 2
        assert "tax document" in got["gmo"][0]["analysis_reason"]

    def test_the_biggest_cluster_comes_first(self, tmp_path, monkeypatch):
        rows = [_declined("gmo", 2)] + [_declined("lazard-am", 2, i=i) for i in range(3)]
        assert [sid for sid, _ in self._run(tmp_path, monkeypatch, rows)] == ["lazard-am", "gmo"]

    def test_old_declines_do_not_alert_forever(self, tmp_path, monkeypatch):
        rows = [_declined("lazard-am", gfh.ZERO_FETCH_FRESH_HOURS + 5)]
        assert self._run(tmp_path, monkeypatch, rows) == []

    def test_summarised_and_other_rows_are_ignored(self, tmp_path, monkeypatch):
        ok = {"id": "x", "source_id": "aqr", "summarized": True, "analysis_checked_at": _hours_ago(1)}
        assert self._run(tmp_path, monkeypatch, [ok]) == []

    def test_retired_sources_are_ignored(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, [_declined("pgim", 1)]) == []

    def test_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        assert gfh.recent_analysis_declines(tmp_path / "missing.jsonl") == []
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{not json\n")
        assert gfh.recent_analysis_declines(bad) == []


class TestDeclinesReachTheEmail:
    DECLINES = [("lazard-am", [_declined("lazard-am", 2, i=0), _declined("lazard-am", 2, i=1)]),
                ("gmo", [_declined("gmo", 2, reason="The text is a health coverage tax document.")])]

    def test_declines_alone_send_the_email(self):
        assert gfh.should_email(NO_ALERTS, [], False, declines=self.DECLINES) is True
        assert gfh.should_email(NO_ALERTS, [], False, declines=[]) is False

    def test_subject_names_them(self):
        subject = gfh.alerts_subject(NO_ALERTS, [], False, declines=self.DECLINES)
        assert "all OK" not in subject
        assert "3" in subject and "lazard-am" in subject

    def test_body_lists_each_source_with_a_reason(self):
        html = gfh.render_html_email({}, NO_ALERTS, {"sources": {}}, 1.0, declines=self.DECLINES)
        assert "lazard-am" in html and "gmo" in html
        assert "health coverage tax document" in html
        assert "NOT SUMMARISED" in html

    def test_reason_text_is_escaped(self):
        d = [("aqr", [_declined("aqr", 1, reason="<script>alert(1)</script>")])]
        html = gfh.render_html_email({}, NO_ALERTS, {"sources": {}}, 1.0, declines=d)
        assert "<script>alert(1)</script>" not in html

    def test_main_reads_and_passes_declines(self):
        """Wiring: the helpers above are useless if main() never calls them."""
        import inspect
        src = inspect.getsource(gfh.main)
        assert "recent_analysis_declines(" in src
        assert src.count("declines=declines") >= 3, "declines not passed to should_email, subject and body"
