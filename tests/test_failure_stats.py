"""scripts/failure_stats.py: counts of labelled failures, by source and label."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("failure_stats", REPO / "scripts" / "failure_stats.py")
fs = importlib.util.module_from_spec(spec)
sys.modules["failure_stats"] = fs
spec.loader.exec_module(fs)

BJT = timezone(timedelta(hours=8))


def _ago(days):
    return (datetime.now(BJT) - timedelta(days=days)).isoformat(timespec="seconds")


ROWS = [
    {"id": "1", "source_id": "matthews-asia", "content_status": "permafail",
     "content_failure": {"label": "media_without_text", "detail": "video"}},
    {"id": "2", "source_id": "matthews-asia", "content_status": "permafail",
     "content_failure": {"label": "media_without_text", "detail": "video"}},
    {"id": "3", "source_id": "metlife-im", "content_status": "permafail",
     "content_failure": {"label": "page_gone", "detail": "redirected"}},
    {"id": "4", "source_id": "gsam", "content_status": "failed"},                        # never labelled
    {"id": "5", "source_id": "ark-invest", "content_status": "metadata_only", "analysis_status": "insufficient_content",
     "analysis_reason": "metadata holds only a title; nothing to summarise"},                # label derived
    {"id": "6", "source_id": "lazard-am", "content_status": "ok", "analysis_status": "insufficient_content",
     "analysis_reason": "legal disclaimer", "analysis_label": "disclaimer_only"},
    {"id": "7", "source_id": "retired", "content_status": "permafail",
     "content_failure": {"label": "page_gone"}},
    {"id": "8", "source_id": "gsam", "content_status": "ok", "summarized": True},
]


def test_current_content_failures_by_source_and_label():
    got = fs.current_content_failures(ROWS, configured={"matthews-asia", "metlife-im", "gsam", "ark-invest", "lazard-am"})
    assert got[("matthews-asia", "media_without_text")] == 2
    assert got[("metlife-im", "page_gone")] == 1
    assert got[("gsam", "unlabelled")] == 1
    assert ("retired", "page_gone") not in got


def test_current_analysis_declines_derive_missing_labels():
    got = fs.current_analysis_declines(ROWS, configured={"ark-invest", "lazard-am"})
    assert got == {("ark-invest", "title_only"): 1, ("lazard-am", "disclaimer_only"): 1}


def test_ledger_window_and_first_seen(tmp_path):
    ledger = tmp_path / "content-failures.jsonl"
    events = [
        {"source_id": "gsam", "label": "fetch_error", "at": _ago(40)},
        {"source_id": "gsam", "label": "fetch_error", "at": _ago(3)},
        {"source_id": "aqr", "label": "selector_miss", "at": _ago(1)},
    ]
    ledger.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n")
    counts, new = fs.ledger_summary(ledger, days=7)
    assert counts == {("gsam", "fetch_error"): 1, ("aqr", "selector_miss"): 1}
    assert new == [("aqr", "selector_miss")], "a (source, label) pair not seen before the window"


def test_report_renders_every_section(tmp_path):
    ledger = tmp_path / "missing.jsonl"
    text = fs.render_report(ROWS, configured={"matthews-asia", "metlife-im", "gsam", "ark-invest", "lazard-am"},
                            ledger=ledger, days=7)
    for heading in ("Content failures", "Analysis declines", "Last 7 days"):
        assert heading in text
    assert "media_without_text" in text and "视频或播客页" in text


class TestQualitySummary:
    CONFIGURED = {"aqr", "gmo", "gsam", "oaktree"}

    def _rows(self):
        return [
            {"id": "n1", "source_id": "aqr", "fetched_at": _ago(2), "content_status": "ok", "summarized": True},
            {"id": "n2", "source_id": "aqr", "fetched_at": _ago(2), "content_status": "failed",
             "content_failure": {"label": "selector_miss"}},
            {"id": "n3", "source_id": "gsam", "fetched_at": _ago(1), "content_status": "metadata_only"},
            {"id": "n4", "source_id": "gmo", "fetched_at": _ago(1), "content_status": "ok",
             "analysis_status": "insufficient_content", "analysis_label": "wrong_document",
             "analysis_checked_at": _ago(1)},
            {"id": "o1", "source_id": "gmo", "fetched_at": _ago(60), "content_status": "ok",
             "analysis_status": "insufficient_content", "analysis_label": "duplicate_body",
             "analysis_checked_at": _ago(60)},
            {"id": "o2", "source_id": "oaktree", "fetched_at": _ago(3), "content_status": "ok",
             "analysis_status": "insufficient_content", "analysis_label": "duplicate_body",
             "analysis_checked_at": _ago(3)},
            {"id": "o3", "source_id": "oaktree", "fetched_at": _ago(90), "content_status": "ok",
             "analysis_status": "insufficient_content", "analysis_label": "duplicate_body",
             "analysis_checked_at": _ago(90)},
        ]

    def test_recent_intake_counts_the_window_only(self):
        got = fs.recent_intake(self._rows(), self.CONFIGURED, days=7)
        assert got == {"total": 5, "with_body": 4, "metadata_only": 1, "without_body": 1, "declined": 2}

    def test_analysis_first_seen_needs_no_earlier_row(self):
        got = fs.analysis_first_seen(self._rows(), self.CONFIGURED, days=7)
        assert ("gmo", "wrong_document") in got
        assert ("oaktree", "duplicate_body") not in got, "oaktree had the same decline 90 days ago"

    def test_quality_summary_alerts_only_on_alertable_new_pairs(self, tmp_path):
        ledger = tmp_path / "content-failures.jsonl"
        ledger.write_text("\n".join(json.dumps(e) for e in [
            {"source_id": "aqr", "label": "selector_miss", "at": _ago(2)},
            {"source_id": "gsam", "label": "media_without_text", "at": _ago(1)},     # new but not alertable
        ]) + "\n")
        q = fs.quality_summary(self._rows(), self.CONFIGURED, ledger, days=7)
        assert ("content", "aqr", "selector_miss") in q["alerts"]
        assert ("analysis", "gmo", "wrong_document") in q["alerts"]
        assert not any(a[2] == "media_without_text" for a in q["alerts"])
        assert q["intake"]["total"] == 5

    def test_an_alert_is_raised_once_not_every_day_of_the_window(self, tmp_path):
        """First-seen inside the 7-day window alone would repeat the alert on
        every daily email for a week (the same flaw 5162b27 fixed for
        declines). With since = the previous health run, a pair first seen
        before that run is not alerted again."""
        ledger = tmp_path / "content-failures.jsonl"
        ledger.write_text(json.dumps({"source_id": "aqr", "label": "selector_miss", "at": _ago(2)}) + "\n")
        q = fs.quality_summary(self._rows(), self.CONFIGURED, ledger, days=7, since=_ago(0.5))
        assert q["alerts"] == [], "gmo wrong_document (1 day ago) and aqr selector_miss (2 days ago) were already reported"
        q = fs.quality_summary(self._rows(), self.CONFIGURED, ledger, days=7, since=_ago(1.5))
        assert q["alerts"] == [("analysis", "gmo", "wrong_document")]

