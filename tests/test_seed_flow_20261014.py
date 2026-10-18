"""Seeds that never move, and new seeds that are never looked for (2026-10-14).

Six seeds sat 18-52 days: discover_fund_sites.py found no research link on
their homepage (or got a 403), so they never got a research_url, and the stall
sweep only moved seeds that had one. Three were rated HIGH. They now go to the
weekly synthesis queue after NO_URL_SEED_DAYS, behind what was already there.

New seeds: candidate-discovery/program.md only searched for new funds when
every candidate had been analysed within 7 days, which with 72 candidates
seldom held; on 09-17..10-13 the agent searched on about 5 days. Phase 3 now
has a fixed budget every session, and the report shows how long since the
last new candidate, counted from added_at (stamped by the guard, not the
agent).
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import detect_stalled_candidates as dsc
import guard_candidate_status as guard
import status_util
import synthesize_fetchers

REPO = Path(__file__).resolve().parent.parent
BJT = timezone(timedelta(hours=8))


def _since(days_ago):
    return (datetime.now(BJT) - timedelta(days=days_ago)).isoformat()


# ── a seed with no research_url is routed to synthesis ───────────────────────

def test_a_seed_without_research_url_goes_to_synthesis_after_a_week():
    c = [{"id": "a", "status": "seed", "status_since": _since(dsc.NO_URL_SEED_DAYS), "notes": ""}]
    actions = dsc.find_stalled(c, threshold_days=3)
    assert [(a["id"], a["new_status"], a["needs_playwright"]) for a in actions] == [("a", "inaccessible", False)]
    dsc.apply_stall_actions(c, actions)
    assert c[0]["status"] == "inaccessible" and "needs_playwright" not in c[0]


def test_it_keeps_its_daily_retries_for_a_week_first():
    c = [{"id": "a", "status": "seed", "status_since": _since(dsc.NO_URL_SEED_DAYS - 1)}]
    assert dsc.find_stalled(c, threshold_days=3) == []


# ── synthesis serves the queue in arrival order ──────────────────────────────

def test_synthesis_takes_the_longest_waiting_first(monkeypatch):
    monkeypatch.setattr(synthesize_fetchers, "load_candidates", lambda: [
        {"id": "new-seed", "status": "inaccessible", "quality": "HIGH", "status_since": _since(0)},
        {"id": "rbc", "status": "inaccessible", "quality": "HIGH", "status_since": _since(7)},
        {"id": "pw", "status": "inaccessible", "quality": "MEDIUM", "status_since": _since(0),
         "needs_playwright": True},
    ])
    monkeypatch.setattr(synthesize_fetchers, "load_fetcher_ids", lambda: set())
    assert [t["id"] for t in synthesize_fetchers.list_targets()] == ["pw", "rbc", "new-seed"]


# ── added_at, stamped by the guard ───────────────────────────────────────────

def test_the_guard_dates_a_new_candidate_and_leaves_old_ones_alone():
    before = [{"id": "old", "status": "seed", "added_at": "2026-08-01"}]
    after = before + [{"id": "new", "status": "seed"}]
    after = [dict(x) for x in after]
    guard.stamp_legal_status_changes(before, after, [])
    assert after[0]["added_at"] == "2026-08-01"
    assert after[1]["added_at"] == datetime.now(BJT).strftime("%Y-%m-%d")


def test_days_since_the_last_new_candidate():
    today = datetime(2026, 10, 14, 9, 0, tzinfo=BJT)
    cands = [{"id": "a", "added_at": "2026-10-01"}, {"id": "b", "added_at": "2026-10-07"}, {"id": "c"}]
    assert status_util.days_since_last_new_candidate(cands, today) == 7
    assert status_util.days_since_last_new_candidate([{"id": "c"}], today) is None


def test_every_candidate_on_disk_has_added_at():
    cands = json.loads((REPO / "config" / "fund_candidates.json").read_text())
    assert [c["id"] for c in cands if not c.get("added_at")] == []


def test_the_report_shows_the_gap():
    text = (REPO / "scripts" / "wrapper-candidate-discovery.sh").read_text()
    assert "days_since_last_new_candidate(candidates" in text


# ── Phase 3 runs every session ───────────────────────────────────────────────

def test_phase_3_no_longer_waits_for_every_candidate_to_be_analysed():
    playbook = (REPO / "candidate-discovery" / "program.md").read_text()
    phase3 = playbook.split("## Phase 3", 1)[1].split("## Phase 4", 1)[0]
    assert "If ALL existing candidates" not in phase3
    assert "every session" in phase3.lower()


def test_the_wrapper_and_the_playbook_agree_on_one_seed():
    text = (REPO / "scripts" / "wrapper-candidate-discovery.sh").read_text()
    # Since 2026-10-18 the wrapper states no numbers of its own; the playbook's
    # Constraints are the session's limits (they had drifted: 5/10 vs 3/8).
    assert "max 2 new seeds" not in text and "max 1 new seed" not in text
    assert "limits under Constraints above are this session's limits" in text
    program = (REPO / "candidate-discovery" / "program.md").read_text()
    assert "Maximum 1 new seed per session" in program
