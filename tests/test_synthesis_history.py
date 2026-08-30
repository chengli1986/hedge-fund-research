"""Tests for fetcher-synthesis history reconcile + stats.

Without these we can't measure whether fetcher-synthesis is working — every
run only writes to fund_candidates.json (latest snapshot only). The reconcile
script extracts a time series; stats answers "30-day success rate".
"""

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load both scripts as modules
sync_spec = importlib.util.spec_from_file_location(
    "sync_history", REPO / "scripts" / "sync_synthesis_history.py")
sync_mod = importlib.util.module_from_spec(sync_spec)
sys.modules["sync_history"] = sync_mod
sync_spec.loader.exec_module(sync_mod)

stats_spec = importlib.util.spec_from_file_location(
    "fs_stats", REPO / "scripts" / "fetcher_synthesis_stats.py")
stats_mod = importlib.util.module_from_spec(stats_spec)
sys.modules["fs_stats"] = stats_mod
stats_spec.loader.exec_module(stats_mod)


def _make_candidates_file(path: Path, candidates: list[dict]):
    path.write_text(json.dumps(candidates, indent=2))


def _utc_iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_reconcile_appends_new_entries(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "Fund A", "status": "visitable",
         "synthesis_attempted_at": _utc_iso(0),
         "synthesis_outcome": "success", "quality": "HIGH"},
        {"id": "fund-b", "name": "Fund B", "status": "inaccessible",
         "synthesis_attempted_at": _utc_iso(2),
         "synthesis_outcome": "failed", "quality": "MEDIUM"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate",
                        lambda *a, **k: "deadbee")

    result = sync_mod.reconcile(lookback_days=7)
    assert result["appended_count"] == 2
    lines = history_file.read_text().splitlines()
    assert len(lines) == 2
    entries = [json.loads(line) for line in lines]
    assert {e["id"] for e in entries} == {"fund-a", "fund-b"}


def test_reconcile_is_idempotent(monkeypatch, tmp_path):
    """Running reconcile twice on same data must NOT duplicate entries."""
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "Fund A", "status": "visitable",
         "synthesis_attempted_at": _utc_iso(0),
         "synthesis_outcome": "success"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    sync_mod.reconcile(lookback_days=7)
    first_lines = history_file.read_text().splitlines()
    sync_mod.reconcile(lookback_days=7)
    second_lines = history_file.read_text().splitlines()
    assert first_lines == second_lines, "reconcile must be idempotent"


def test_reconcile_skips_old_attempts(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "ancient-fund", "name": "Ancient", "status": "inaccessible",
         "synthesis_attempted_at": _utc_iso(30),  # past 7-day lookback
         "synthesis_outcome": "failed"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    result = sync_mod.reconcile(lookback_days=7)
    assert result["appended_count"] == 0


def test_reconcile_skips_candidates_without_attempt(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "untried", "name": "Untried", "status": "visitable"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    result = sync_mod.reconcile(lookback_days=7)
    assert result["appended_count"] == 0


def test_reconcile_handles_dict_wrapped_candidates(monkeypatch, tmp_path):
    """fund_candidates.json sometimes ships as {"candidates": [...]} not bare list."""
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    cand_file.write_text(json.dumps({
        "candidates": [
            {"id": "fund-a", "name": "Fund A", "status": "visitable",
             "synthesis_attempted_at": _utc_iso(0),
             "synthesis_outcome": "success"},
        ]
    }))
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")
    result = sync_mod.reconcile(lookback_days=7)
    assert result["appended_count"] == 1


# ── stats tests ────────────────────────────────────────────────────────────────

def test_stats_empty_history():
    result = stats_mod.compute_stats([], window_days=30)
    assert result["total"] == 0
    assert result["success_rate"] is None


def test_stats_basic_rate():
    entries = [
        {"id": "a", "outcome": "success", "attempted_at": _utc_iso(0), "quality": "HIGH"},
        {"id": "b", "outcome": "success", "attempted_at": _utc_iso(5), "quality": "MEDIUM"},
        {"id": "c", "outcome": "failed", "attempted_at": _utc_iso(2), "quality": "HIGH"},
        {"id": "d", "outcome": "failed", "attempted_at": _utc_iso(10), "quality": "LOW"},
    ]
    result = stats_mod.compute_stats(entries, window_days=30)
    assert result["success"] == 2
    assert result["failed"] == 2
    assert result["total"] == 4
    assert result["success_rate"] == 0.5


def test_stats_excludes_outside_window():
    entries = [
        {"id": "recent", "outcome": "success", "attempted_at": _utc_iso(5)},
        {"id": "old", "outcome": "failed", "attempted_at": _utc_iso(60)},
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    assert r["total"] == 1
    assert r["success"] == 1


def test_stats_per_quality_breakdown():
    entries = [
        {"id": "a", "outcome": "success", "attempted_at": _utc_iso(1), "quality": "HIGH"},
        {"id": "b", "outcome": "failed", "attempted_at": _utc_iso(1), "quality": "HIGH"},
        {"id": "c", "outcome": "success", "attempted_at": _utc_iso(1), "quality": "MEDIUM"},
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    assert r["per_quality"]["HIGH"]["success"] == 1
    assert r["per_quality"]["HIGH"]["total"] == 2
    assert r["per_quality"]["HIGH"]["rate"] == 0.5
    assert r["per_quality"]["MEDIUM"]["success"] == 1
    assert r["per_quality"]["MEDIUM"]["total"] == 1
    assert r["per_quality"]["LOW"]["total"] == 0


def test_stats_handles_unparseable_dates_gracefully():
    entries = [
        {"id": "broken", "outcome": "success", "attempted_at": "not-a-date"},
        {"id": "good", "outcome": "success", "attempted_at": _utc_iso(1)},
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    assert r["total"] == 1


def test_format_rate_helper():
    assert stats_mod.format_rate(0.5) == "50.0%"
    assert stats_mod.format_rate(None) == "n/a"
    assert stats_mod.format_rate(0.0) == "0.0%"


# ── needs_playwright breakdown (B-tier metric) ────────────────────────────────

def test_reconcile_writes_needs_playwright_field(monkeypatch, tmp_path):
    """The metric whose collection we're enabling — must end up in history.jsonl."""
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "A", "status": "visitable",
         "synthesis_attempted_at": _utc_iso(0), "synthesis_outcome": "success",
         "needs_playwright": True},
        {"id": "fund-b", "name": "B", "status": "visitable",
         "synthesis_attempted_at": _utc_iso(0), "synthesis_outcome": "success"},
        # ^ no needs_playwright field
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    sync_mod.reconcile(lookback_days=7)
    lines = [json.loads(line) for line in history_file.read_text().splitlines()]
    by_id = {e["id"]: e for e in lines}
    assert by_id["fund-a"]["needs_playwright"] is True
    assert by_id["fund-b"]["needs_playwright"] is False  # absent → False


def test_stats_breakdown_separates_cohorts():
    entries = [
        {"id": "a", "outcome": "success", "attempted_at": _utc_iso(1),
         "needs_playwright": True, "quality": "HIGH"},
        {"id": "b", "outcome": "failed", "attempted_at": _utc_iso(1),
         "needs_playwright": True, "quality": "HIGH"},
        {"id": "c", "outcome": "success", "attempted_at": _utc_iso(1),
         "needs_playwright": False, "quality": "HIGH"},
        {"id": "d", "outcome": "success", "attempted_at": _utc_iso(1),
         "needs_playwright": False, "quality": "MEDIUM"},
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    assert r["needs_playwright_breakdown"]["needs_playwright"]["success"] == 1
    assert r["needs_playwright_breakdown"]["needs_playwright"]["total"] == 2
    assert r["needs_playwright_breakdown"]["needs_playwright"]["rate"] == 0.5
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["success"] == 2
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["total"] == 2
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["rate"] == 1.0


def test_stats_breakdown_treats_missing_field_as_false():
    """Old entries (pre-feature) must default to no_playwright_flag bucket."""
    entries = [
        {"id": "old", "outcome": "success", "attempted_at": _utc_iso(1)},
        # ^ no needs_playwright key
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["total"] == 1
    assert r["needs_playwright_breakdown"]["needs_playwright"]["total"] == 0


def test_stats_breakdown_handles_empty_cohort_gracefully():
    entries = [
        {"id": "a", "outcome": "success", "attempted_at": _utc_iso(1),
         "needs_playwright": True},
    ]
    r = stats_mod.compute_stats(entries, window_days=30)
    # needs_playwright=False cohort empty → rate is None (not a crash)
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["total"] == 0
    assert r["needs_playwright_breakdown"]["no_playwright_flag"]["rate"] is None


# ── date field: BJT, so the log lines up with the digest email and the cron ──

def test_reconcile_dates_entry_in_bjt_not_utc(monkeypatch, tmp_path):
    """The Sunday 02:00 BJT run happens at 18:00 UTC Saturday. Dating the row
    in UTC labels it Saturday, so the date in the digest email (BJT) can't be
    grepped in the log. Date rows in BJT to match email + cron schedule."""
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    attempted = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
        hour=18, minute=2, second=24, microsecond=0).isoformat()
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "Fund A", "status": "visitable",
         "synthesis_attempted_at": attempted,
         "synthesis_outcome": "success", "quality": "MEDIUM"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    sync_mod.reconcile(lookback_days=7)
    entry = json.loads(history_file.read_text().splitlines()[0])
    expected = datetime.fromisoformat(attempted).astimezone(
        timezone(timedelta(hours=8))).date().isoformat()
    assert entry["date"] == expected


def test_reconcile_does_not_duplicate_row_already_logged_under_utc_date(
        monkeypatch, tmp_path):
    """Rows written before the BJT switch carry the UTC date. Re-running
    reconcile must recognise them, not append a second row under the BJT date."""
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    attempted = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
        hour=18, minute=2, second=24, microsecond=0).isoformat()
    utc_date = datetime.fromisoformat(attempted).date().isoformat()
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps({
        "date": utc_date, "attempted_at": attempted, "id": "fund-a",
        "name": "Fund A", "outcome": "success"}) + "\n")
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "Fund A", "status": "visitable",
         "synthesis_attempted_at": attempted,
         "synthesis_outcome": "success", "quality": "MEDIUM"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    result = sync_mod.reconcile(lookback_days=7)
    assert result["appended_count"] == 0
    assert len(history_file.read_text().splitlines()) == 1


# ── failure reason travels from the candidate into the log row ───────────────

def test_reconcile_carries_synthesis_failure_reason(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "fund-b", "name": "Fund B", "status": "inaccessible",
         "synthesis_attempted_at": _utc_iso(0), "synthesis_outcome": "failed",
         "synthesis_failure_reason": "index selector matched 0 links",
         "notes": "discovery-time note that is NOT the failure cause",
         "quality": "MEDIUM"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    sync_mod.reconcile(lookback_days=7)
    entry = json.loads(history_file.read_text().splitlines()[0])
    assert entry["failure_reason"] == "index selector matched 0 links"


def test_reconcile_omits_failure_reason_on_success(monkeypatch, tmp_path):
    cand_file = tmp_path / "fund_candidates.json"
    history_file = tmp_path / "logs" / "history.jsonl"
    _make_candidates_file(cand_file, [
        {"id": "fund-a", "name": "Fund A", "status": "visitable",
         "synthesis_attempted_at": _utc_iso(0), "synthesis_outcome": "success",
         "quality": "HIGH"},
    ])
    monkeypatch.setattr(sync_mod, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(sync_mod, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sync_mod, "_commit_sha_for_candidate", lambda *a, **k: "")

    sync_mod.reconcile(lookback_days=7)
    entry = json.loads(history_file.read_text().splitlines()[0])
    assert "failure_reason" not in entry
