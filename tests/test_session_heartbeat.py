"""Tests for fetcher-synthesis session heartbeat — the 'monitor the monitor'.

Without these, an agent that crashes mid-update or stops writing
fund_candidates.json fields produces zero history entries; stats lie ('0 attempts')
and no one notices. Heartbeat is a session_end marker that ALWAYS writes,
regardless of agent behavior, plus an inconsistency detector for
'agent ran but recorded nothing'.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "write_session_heartbeat.py"

spec = importlib.util.spec_from_file_location("hb", SCRIPT)
hb = importlib.util.module_from_spec(spec)
sys.modules["hb"] = hb
spec.loader.exec_module(hb)

# Also load fetcher_synthesis_stats to verify heartbeats don't pollute stats
fs_spec = importlib.util.spec_from_file_location(
    "fs_stats_hb", REPO / "scripts" / "fetcher_synthesis_stats.py")
fs_stats = importlib.util.module_from_spec(fs_spec)
sys.modules["fs_stats_hb"] = fs_stats
fs_spec.loader.exec_module(fs_stats)


def test_heartbeat_writes_jsonl_line(tmp_path):
    log = tmp_path / "history.jsonl"
    entry = hb.write_heartbeat(targets_count=2, reconcile_appended=1,
                               agent_exit=0, history_path=log)
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written["id"] == "_heartbeat"
    assert written["outcome"] == "session_end"
    assert written["targets_count"] == 2
    assert written["reconcile_appended"] == 1
    assert written["agent_exit"] == 0
    assert "date" in written
    assert "timestamp" in written


def test_heartbeat_appends_to_existing_log(tmp_path):
    log = tmp_path / "history.jsonl"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({"id": "previous", "outcome": "success"}) + "\n")

    hb.write_heartbeat(targets_count=0, reconcile_appended=0,
                       agent_exit=0, history_path=log)

    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "previous"
    assert json.loads(lines[1])["id"] == "_heartbeat"


def test_inconsistency_detected_when_agent_ran_but_no_records():
    """The exact case heartbeat exists to catch: targets > 0, exit 0, no records."""
    reason = hb.detect_inconsistency(targets_count=2, reconcile_appended=0,
                                     agent_exit=0)
    assert reason is not None
    assert "0 entries" in reason


def test_no_inconsistency_when_targets_zero():
    """No targets = nothing to do = OK. Common case (run on quiet day)."""
    reason = hb.detect_inconsistency(targets_count=0, reconcile_appended=0,
                                     agent_exit=0)
    assert reason is None


def test_no_inconsistency_when_agent_exited_nonzero():
    """Agent crashed → already a separate alert via exit code, don't double-flag."""
    reason = hb.detect_inconsistency(targets_count=2, reconcile_appended=0,
                                     agent_exit=1)
    assert reason is None


def test_no_inconsistency_when_agent_recorded_outcomes():
    """Healthy run: targets > 0 and reconcile picked up outcomes."""
    reason = hb.detect_inconsistency(targets_count=2, reconcile_appended=2,
                                     agent_exit=0)
    assert reason is None


def test_partial_inconsistency_still_caught():
    """Agent had 5 targets, reconcile got 0 — still inconsistent (silent fail)."""
    reason = hb.detect_inconsistency(targets_count=5, reconcile_appended=0,
                                     agent_exit=0)
    assert reason is not None


def test_heartbeats_do_not_pollute_stats(tmp_path, monkeypatch):
    """Stats must filter heartbeats — they're not synthesis attempts."""
    log = tmp_path / "history.jsonl"
    # Mix real outcomes + heartbeats
    log.parent.mkdir(exist_ok=True)
    log.write_text("\n".join([
        json.dumps({"id": "fund-a", "outcome": "success", "attempted_at":
                    _now_iso(), "quality": "HIGH"}),
        json.dumps({"id": "_heartbeat", "outcome": "session_end",
                    "timestamp": _now_iso(),
                    "targets_count": 1, "reconcile_appended": 1, "agent_exit": 0}),
        json.dumps({"id": "fund-b", "outcome": "failed", "attempted_at":
                    _now_iso(), "quality": "MEDIUM"}),
    ]) + "\n")
    monkeypatch.setattr(fs_stats, "HISTORY_FILE", log)

    entries = fs_stats.load_history()
    ids = [e.get("id") for e in entries]
    assert "_heartbeat" not in ids
    assert "fund-a" in ids
    assert "fund-b" in ids

    # And success rate computation only counts real attempts
    stats = fs_stats.compute_stats(entries, window_days=30)
    assert stats["total"] == 2
    assert stats["success"] == 1


def test_heartbeats_visible_when_explicitly_requested(tmp_path, monkeypatch):
    """include_heartbeats=True for forensic / debugging access."""
    log = tmp_path / "history.jsonl"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({"id": "_heartbeat", "outcome": "session_end"}) + "\n")
    monkeypatch.setattr(fs_stats, "HISTORY_FILE", log)
    entries = fs_stats.load_history(include_heartbeats=True)
    assert len(entries) == 1
    assert entries[0]["id"] == "_heartbeat"


def test_main_returns_1_on_inconsistency(tmp_path, monkeypatch):
    """CLI must exit 1 when inconsistent so wrapper can propagate alert."""
    log = tmp_path / "history.jsonl"
    monkeypatch.setattr(hb, "HISTORY_FILE", log)
    sys_argv_orig = sys.argv
    try:
        sys.argv = ["write_session_heartbeat.py", "--targets-count", "3",
                    "--reconcile-appended", "0", "--agent-exit", "0"]
        rc = hb.main()
        assert rc == 1
    finally:
        sys.argv = sys_argv_orig


def test_main_returns_0_on_consistent_state(tmp_path, monkeypatch):
    log = tmp_path / "history.jsonl"
    monkeypatch.setattr(hb, "HISTORY_FILE", log)
    sys_argv_orig = sys.argv
    try:
        sys.argv = ["write_session_heartbeat.py", "--targets-count", "2",
                    "--reconcile-appended", "2", "--agent-exit", "0"]
        rc = hb.main()
        assert rc == 0
    finally:
        sys.argv = sys_argv_orig
