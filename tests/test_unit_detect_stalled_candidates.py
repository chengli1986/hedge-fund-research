"""Unit tests for detect_stalled_candidates — the post-pipeline sweep that
auto-routes candidates stuck without progress, so they don't sit silently in
seed/screen_failed forever (2026-07-14 Nuveen/Longleaf/Lord Abbett/Invesco
incident: 4 seed candidates stuck for weeks because stage1's httpx crawl has
no JS-render fallback, discovered only by a manual audit).

Two rules, both gated on status_since >= threshold days old:
  - seed (with a confirmed research_url) -> discovered
    (unsticks the stage1 JS-blind-spot case; lets it flow through screen
    again next run instead of waiting forever for stage1 to succeed)
  - screen_failed -> inaccessible + needs_playwright
    (stage2 has been retrying and failing every day without progress; stop
    the pointless static retries and hand it to fetcher-synthesis)
"""
from datetime import datetime, timedelta, timezone

import detect_stalled_candidates as dsc

BJT = timezone(timedelta(hours=8))


def _since(days_ago):
    return (datetime.now(BJT) - timedelta(days=days_ago)).isoformat()


def _c(cid, status, status_since=None, **extra):
    c = {"id": cid, "name": cid, "status": status, "notes": ""}
    if status_since is not None:
        c["status_since"] = status_since
    c.update(extra)
    return c


class TestFindStalled:
    def test_seed_with_research_url_stuck_past_threshold_is_flagged(self):
        candidates = [_c("a", "seed", _since(4), research_url="https://x.com/insights")]
        actions = dsc.find_stalled(candidates, threshold_days=3)
        assert len(actions) == 1
        assert actions[0]["id"] == "a"
        assert actions[0]["new_status"] == "discovered"

    def test_seed_without_research_url_is_never_flagged(self):
        # Nothing confirmed to retry against — not our call to force it forward.
        candidates = [_c("a", "seed", _since(10))]
        assert dsc.find_stalled(candidates, threshold_days=3) == []

    def test_seed_under_threshold_is_not_flagged(self):
        candidates = [_c("a", "seed", _since(1), research_url="https://x.com/insights")]
        assert dsc.find_stalled(candidates, threshold_days=3) == []

    def test_screen_failed_stuck_past_threshold_is_flagged(self):
        candidates = [_c("a", "screen_failed", _since(5))]
        actions = dsc.find_stalled(candidates, threshold_days=3)
        assert len(actions) == 1
        assert actions[0]["id"] == "a"
        assert actions[0]["new_status"] == "inaccessible"
        assert actions[0]["needs_playwright"] is True

    def test_screen_failed_under_threshold_is_not_flagged(self):
        candidates = [_c("a", "screen_failed", _since(2))]
        assert dsc.find_stalled(candidates, threshold_days=3) == []

    def test_other_statuses_never_flagged(self):
        candidates = [
            _c("a", "discovered", _since(30)),
            _c("b", "screened", _since(30)),
            _c("c", "visitable", _since(30)),
            _c("d", "promoted", _since(30)),
            _c("e", "watchlist", _since(30)),
            _c("f", "rejected", _since(30)),
            _c("g", "inaccessible", _since(30)),
        ]
        assert dsc.find_stalled(candidates, threshold_days=3) == []

    def test_missing_status_since_is_skipped_not_crashed(self):
        candidates = [_c("a", "seed", research_url="https://x.com/insights")]
        assert dsc.find_stalled(candidates, threshold_days=3) == []

    def test_exactly_at_threshold_is_flagged(self):
        candidates = [_c("a", "screen_failed", _since(3))]
        actions = dsc.find_stalled(candidates, threshold_days=3)
        assert len(actions) == 1


class TestApplyStallActions:
    def test_seed_action_flips_status_and_tags_note(self):
        candidates = [_c("a", "seed", _since(4), research_url="https://x.com/insights")]
        actions = dsc.find_stalled(candidates, threshold_days=3)
        applied = dsc.apply_stall_actions(candidates, actions)
        assert applied == 1
        assert candidates[0]["status"] == "discovered"
        assert candidates[0]["status_since"] != _since(4)  # re-stamped by set_status
        assert "[auto-stall" in candidates[0]["notes"]

    def test_screen_failed_action_sets_needs_playwright(self):
        candidates = [_c("a", "screen_failed", _since(5))]
        actions = dsc.find_stalled(candidates, threshold_days=3)
        dsc.apply_stall_actions(candidates, actions)
        assert candidates[0]["status"] == "inaccessible"
        assert candidates[0]["needs_playwright"] is True
        assert "[auto-stall" in candidates[0]["notes"]

    def test_no_actions_is_a_noop(self):
        candidates = [_c("a", "promoted", _since(30))]
        applied = dsc.apply_stall_actions(candidates, [])
        assert applied == 0
        assert candidates[0]["status"] == "promoted"


class TestMainIntegration:
    def test_dry_run_does_not_write_file(self, tmp_path, monkeypatch, capsys):
        cand_file = tmp_path / "fund_candidates.json"
        import json
        cand_file.write_text(json.dumps([
            _c("a", "seed", _since(4), research_url="https://x.com/insights"),
        ]))
        monkeypatch.setattr(dsc, "CANDIDATES_FILE", cand_file)
        dsc.main(["--dry-run", "--threshold-days", "3"])
        on_disk = json.loads(cand_file.read_text())
        assert on_disk[0]["status"] == "seed"  # untouched

    def test_live_run_writes_file(self, tmp_path, monkeypatch):
        cand_file = tmp_path / "fund_candidates.json"
        import json
        cand_file.write_text(json.dumps([
            _c("a", "seed", _since(4), research_url="https://x.com/insights"),
            _c("b", "promoted", _since(30)),
        ]))
        monkeypatch.setattr(dsc, "CANDIDATES_FILE", cand_file)
        dsc.main(["--threshold-days", "3"])
        on_disk = json.loads(cand_file.read_text())
        by_id = {c["id"]: c for c in on_disk}
        assert by_id["a"]["status"] == "discovered"
        assert by_id["b"]["status"] == "promoted"
