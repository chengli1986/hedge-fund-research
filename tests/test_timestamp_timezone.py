"""One timezone across the files this pipeline writes.

Four of the five stamp BJT (gmia-fetcher-health.json, articles.jsonl,
template-census.jsonl, ab-gate.jsonl) and inspection_state.json stamped UTC,
which is this machine's clock. Nothing compared them wrongly -- every reader
parses with fromisoformat and every stamp is offset-aware -- but two places
printed the stored string as it was, next to a label saying BJT: the health
page's freshness chip (fixed when it was seen on screen) and the daily
email's zero-fetch line, which reads "2026-09-25T19:45:06" for a fetch that
happened at 03:45 BJT.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fetch_articles

spec = importlib.util.spec_from_file_location("gfh_tz", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_tz"] = gfh
spec.loader.exec_module(gfh)

BJT = timezone(timedelta(hours=8))


class TestWriterStampsBjt:
    def test_quality_metrics_record_a_bjt_timestamp(self, tmp_path, monkeypatch):
        state = tmp_path / "inspection_state.json"
        monkeypatch.setattr(fetch_articles, "INSPECTION_STATE_FILE", state)
        fetch_articles.record_quality_metrics("alpha", 10, 1, 0, 0)
        stamp = json.loads(state.read_text())["alpha"]["last_inspected_at"]
        assert datetime.fromisoformat(stamp).utcoffset() == timedelta(hours=8), stamp

    def test_the_stamp_is_the_same_instant_as_utc_would_have_been(self, tmp_path, monkeypatch):
        state = tmp_path / "inspection_state.json"
        monkeypatch.setattr(fetch_articles, "INSPECTION_STATE_FILE", state)
        before = datetime.now(timezone.utc)
        fetch_articles.record_quality_metrics("alpha", 10, 1, 0, 0)
        stamp = datetime.fromisoformat(json.loads(state.read_text())["alpha"]["last_inspected_at"])
        assert before <= stamp <= datetime.now(timezone.utc)


class TestReadersPrintBjt:
    def test_the_zero_fetch_line_prints_bjt_not_the_stored_offset(self):
        """A UTC stamp left over from before the switch must still read BJT."""
        line = gfh._zero_fetch_line("alpha", {"consecutive_zero_count": 2,
                                              "last_inspected_at": "2026-09-25T19:45:06+00:00"})
        assert "2026-09-26 03:45" in line and "19:45" not in line

    def test_a_bjt_stamp_is_printed_unchanged(self):
        line = gfh._zero_fetch_line("alpha", {"consecutive_zero_count": 2,
                                              "last_inspected_at": "2026-09-26T03:45:06+08:00"})
        assert "2026-09-26 03:45" in line

    def test_an_unreadable_stamp_does_not_break_the_line(self):
        line = gfh._zero_fetch_line("alpha", {"consecutive_zero_count": 1,
                                              "last_inspected_at": "not a date"})
        assert "alpha" in line or "run" in line
