"""Tests for backfill_failed_synthesis — auto-mark unprocessed synthesis targets as failed.

Without this, a fetcher-synthesis agent that silently skips a target (never
writing synthesis_outcome) leaves the candidate `inaccessible` forever:
synthesize_fetchers.auto_reject_exhausted_candidates never counts a failure, so
the candidate re-queues every week with no history entry and no alert — the
franklin-templeton "stuck 8 weeks" failure mode.
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "backfill_failed", REPO / "scripts" / "backfill_failed_synthesis.py")
bf = importlib.util.module_from_spec(_spec)
sys.modules["backfill_failed"] = bf
_spec.loader.exec_module(bf)


def _utc_iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _make_candidates(path: Path, candidates: list[dict]):
    path.write_text(json.dumps(candidates, indent=2))


def test_backfills_unprocessed_target_as_failed(monkeypatch, tmp_path):
    """Planned target still inaccessible with no this-session attempt → marked failed."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(minutes_ago=10)
    _make_candidates(cand_file, [
        {"id": "franklin-templeton", "name": "Franklin", "status": "inaccessible",
         "quality": "MEDIUM", "synthesis_attempted_at": None, "synthesis_outcome": None},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)
    result = bf.backfill(planned_ids=["franklin-templeton"], run_start=run_start)
    assert result["backfilled_count"] == 1
    assert "franklin-templeton" in result["backfilled"]
    data = {c["id"]: c for c in json.loads(cand_file.read_text())}
    assert data["franklin-templeton"]["synthesis_outcome"] == "failed"
    assert data["franklin-templeton"]["synthesis_attempted_at"] is not None


def test_backfills_stale_attempt_from_prior_session(monkeypatch, tmp_path):
    """Attempt timestamp predates this session's start → treated as unprocessed → failed."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(minutes_ago=10)
    stale = _utc_iso(minutes_ago=60 * 24 * 7)  # a week ago, before this run
    _make_candidates(cand_file, [
        {"id": "franklin-templeton", "name": "Franklin", "status": "inaccessible",
         "quality": "MEDIUM", "synthesis_attempted_at": stale, "synthesis_outcome": "failed"},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)
    result = bf.backfill(planned_ids=["franklin-templeton"], run_start=run_start)
    assert result["backfilled_count"] == 1


def test_does_not_backfill_agent_marked_target(monkeypatch, tmp_path):
    """Agent already wrote synthesis_attempted_at this session → not touched."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(minutes_ago=10)
    agent_stamp = _utc_iso(minutes_ago=2)  # after run_start
    _make_candidates(cand_file, [
        {"id": "cohen-steers", "name": "Cohen", "status": "inaccessible",
         "quality": "MEDIUM", "synthesis_attempted_at": agent_stamp,
         "synthesis_outcome": "failed"},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)
    result = bf.backfill(planned_ids=["cohen-steers"], run_start=run_start)
    assert result["backfilled_count"] == 0
    data = {c["id"]: c for c in json.loads(cand_file.read_text())}
    assert data["cohen-steers"]["synthesis_attempted_at"] == agent_stamp  # unchanged


def test_does_not_backfill_succeeded_target(monkeypatch, tmp_path):
    """Agent succeeded → status moved off inaccessible → not marked failed."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(minutes_ago=10)
    _make_candidates(cand_file, [
        {"id": "principal-am", "name": "Principal", "status": "visitable",
         "quality": "MEDIUM", "synthesis_attempted_at": None, "synthesis_outcome": None},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)
    result = bf.backfill(planned_ids=["principal-am"], run_start=run_start)
    assert result["backfilled_count"] == 0


def test_ignores_non_planned_candidates(monkeypatch, tmp_path):
    """Only planned ids are considered — a stuck inaccessible fund not in this session is skipped."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(minutes_ago=10)
    _make_candidates(cand_file, [
        {"id": "not-planned", "name": "X", "status": "inaccessible",
         "quality": "MEDIUM", "synthesis_attempted_at": None, "synthesis_outcome": None},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)
    result = bf.backfill(planned_ids=["franklin-templeton"], run_start=run_start)
    assert result["backfilled_count"] == 0


def test_backfill_records_the_verified_failure_reason(monkeypatch, tmp_path):
    """Backfill knows exactly why it marked the target failed — the agent never
    recorded an attempt this session. Record that, so the digest email has a
    real cause instead of falling back to the shared `notes` field."""
    cand_file = tmp_path / "fund_candidates.json"
    run_start = _utc_iso(30)
    _make_candidates(cand_file, [
        {"id": "fund-x", "name": "Fund X", "status": "inaccessible",
         "notes": "discovery-time note that is NOT the failure cause"},
    ])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)

    bf.backfill(["fund-x"], run_start, candidates_path=cand_file)
    c = json.loads(cand_file.read_text())[0]
    assert c["synthesis_outcome"] == "failed"
    reason = c["synthesis_failure_reason"]
    assert "记录尝试" in reason and "backfill" in reason  # names what was verified
    assert "discovery-time note" not in reason           # never the shared notes field
    assert c["notes"] == "discovery-time note that is NOT the failure cause"


def test_backfill_reason_fits_the_digest_cell_without_truncation(monkeypatch, tmp_path):
    """The digest shows at most 120 chars of a failure reason. A reason longer
    than that gets cut mid-path and loses the log pointer that makes it
    actionable — keep it inside the budget."""
    cand_file = tmp_path / "fund_candidates.json"
    _make_candidates(cand_file, [
        {"id": "fund-x", "name": "Fund X", "status": "inaccessible"}])
    monkeypatch.setattr(bf, "CANDIDATES_FILE", cand_file)

    bf.backfill(["fund-x"], _utc_iso(30), candidates_path=cand_file)
    reason = json.loads(cand_file.read_text())[0]["synthesis_failure_reason"]
    assert reason.endswith("gmia-fetcher-synthesis.log")
    assert len(reason) <= 120, f"reason is {len(reason)} chars, will be truncated"
