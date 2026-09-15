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
