"""Unit tests for the GMIA pipeline liveness audit (pure logic only — no I/O)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gmia_liveness_audit as la  # noqa: E402

NOW = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)


# --- parse_iso -------------------------------------------------------------
def test_parse_iso_handles_z_suffix():
    assert la.parse_iso("2026-07-20T00:00:00Z") == datetime(
        2026, 7, 20, tzinfo=timezone.utc)


def test_parse_iso_naive_becomes_utc():
    assert la.parse_iso("2026-07-20T00:00:00").tzinfo == timezone.utc


def test_parse_iso_invalid_returns_none():
    assert la.parse_iso("not-a-date") is None
    assert la.parse_iso("") is None


# --- newest_dt -------------------------------------------------------------
def test_newest_dt_picks_latest():
    rows = [{"t": "2026-07-01T00:00:00+00:00"}, {"t": "2026-07-19T00:00:00+00:00"},
            {"t": "2026-07-10T00:00:00+00:00"}]
    assert la.newest_dt(rows, "t") == datetime(2026, 7, 19, tzinfo=timezone.utc)


def test_newest_dt_empty_is_none():
    assert la.newest_dt([], "t") is None
    assert la.newest_dt([{"other": "x"}], "t") is None


# --- classify_staleness ----------------------------------------------------
def test_classify_ok_within_threshold():
    last = NOW - timedelta(days=1)
    status, age = la.classify_staleness(NOW, last, threshold_days=4)
    assert status == la.OK and round(age) == 1


def test_classify_stale_over_threshold():
    last = NOW - timedelta(days=10)
    status, age = la.classify_staleness(NOW, last, threshold_days=9)
    assert status == la.STALE and round(age) == 10


def test_classify_stale_when_never_seen():
    status, age = la.classify_staleness(NOW, None, threshold_days=4)
    assert status == la.STALE and age is None


# --- scan_markers ----------------------------------------------------------
def test_scan_finds_lock_bail():
    text = "[2026-07-19 02:00:01] Another fetcher-synthesis instance is running. Exiting."
    assert la.scan_markers(text, la.LOCK_BAIL_MARKERS) == ["lock-bail"]


def test_scan_finds_agent_derail():
    text = "我按全局指令尝试做「会话开始·每日日志回顾」... 你想怎么进行？"
    hits = la.scan_markers(text, la.AGENT_DERAIL_MARKERS)
    assert "did-daily-log-ritual" in hits and "stalled-asking-user" in hits


def test_scan_does_not_flag_bare_trust_warning():
    # The critical false-positive guard: a plain "not been trusted" warning is
    # NOT a bail — candidate-discovery logs it every run yet works correctly.
    text = "Ignoring 4 permissions.allow entries: this workspace has not been trusted."
    assert la.scan_markers(text, la.LOCK_BAIL_MARKERS) == []
    assert la.scan_markers(text, la.AGENT_DERAIL_MARKERS) == []


# --- ops_problems ----------------------------------------------------------
def test_ops_flags_locked_timeout_and_bad_exit():
    rows = [
        {"job": "gmia-fetcher-synthesis", "ts": "2026-07-19T18:00:00+00:00", "locked": True},
        {"job": "gmia-daily", "ts": "2026-07-20T01:00:00+00:00", "timeout": True},
        {"job": "gmia-nightly-test", "ts": "2026-07-20T01:30:00+00:00", "exit": 1},
        {"job": "gmia-daily", "ts": "2026-07-20T01:45:00+00:00", "exit": 0},  # clean
    ]
    probs = la.ops_problems(rows, "gmia", NOW, window_days=3)
    jobs = {p["job"]: p["reasons"] for p in probs}
    assert jobs["gmia-fetcher-synthesis"] == ["locked-out"]
    assert jobs["gmia-daily"] == ["timed-out"]  # the exit=0 gmia-daily row is excluded
    assert jobs["gmia-nightly-test"] == ["exit=1"]


def test_ops_ignores_other_prefixes_and_old_runs():
    rows = [
        {"job": "smtp-health", "ts": "2026-07-20T01:00:00+00:00", "exit": 1},  # not gmia
        {"job": "gmia-daily", "ts": "2026-07-01T01:00:00+00:00", "exit": 1},   # too old
    ]
    assert la.ops_problems(rows, "gmia", NOW, window_days=3) == []
