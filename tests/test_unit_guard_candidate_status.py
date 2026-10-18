"""Unit tests for guard_candidate_status — backstop that reverts illegal status
changes made by the candidate-discovery agent.

The discovery agent may only downgrade a candidate to watchlist/rejected, or add
a brand-new candidate as 'seed'. It must NEVER set 'promoted' (or any other
pipeline-owned status) — promotion is gmia-trial-manager's job, only after a trial.
"""
import guard_candidate_status as g


def _c(cid, status):
    return {"id": cid, "status": status, "notes": "x"}


class TestFindIllegalChanges:
    def test_agent_setting_promoted_is_illegal(self):
        before = [_c("a", "discovered")]
        after = [_c("a", "promoted")]
        illegal = g.find_illegal_changes(before, after)
        assert illegal == [{"id": "a", "from": "discovered", "to": "promoted"}]

    def test_downgrade_to_watchlist_is_allowed(self):
        before = [_c("a", "discovered")]
        after = [_c("a", "watchlist")]
        assert g.find_illegal_changes(before, after) == []

    def test_downgrade_to_rejected_is_allowed(self):
        before = [_c("a", "discovered")]
        after = [_c("a", "rejected")]
        assert g.find_illegal_changes(before, after) == []

    def test_unchanged_promoted_is_not_flagged(self):
        # A pre-existing, legitimately-promoted candidate the agent did NOT touch
        # must never be flagged (no false positives on the existing source set).
        before = [_c("a", "promoted")]
        after = [_c("a", "promoted")]
        assert g.find_illegal_changes(before, after) == []

    def test_agent_setting_pipeline_owned_status_is_illegal(self):
        # visitable/inaccessible/screened are owned by specific scripts, not the
        # agent — a bare hand-edit with no supporting fresh timestamp is still
        # flagged (no last_validated_at present at all here).
        before = [_c("a", "discovered")]
        after = [_c("a", "visitable")]
        assert g.find_illegal_changes(before, after) == [
            {"id": "a", "from": "discovered", "to": "visitable"}
        ]

    def test_a_forged_script_timestamp_no_longer_excuses_a_status(self):
        # Stage 6 #9 (2026-10-18): the Phase 1 scripts run before the guard's
        # snapshot now, so inside the session a status change on an existing
        # candidate is the agent's. Setting status together with the script's
        # own timestamp (which used to count as proof) is still illegal.
        # (Before: 2026-07-15 longleaf-partners/lord-abbett/invesco were
        # reverted because the scripts ran inside the window.)
        for frm, to, field in (("discovered", "screened", "last_screened_at"),
                               ("screened", "inaccessible", "last_validated_at"),
                               ("screened", "visitable", "last_validated_at")):
            before = [{"id": "a", "status": frm, "notes": "x"}]
            after = [{"id": "a", "status": to, "notes": "x",
                      field: "2026-10-18T09:00:01+08:00"}]
            assert g.find_illegal_changes(before, after) == [
                {"id": "a", "from": frm, "to": to}]

    def test_the_wrapper_runs_phase_1_before_the_snapshot(self):
        from pathlib import Path
        w = (Path(g.__file__).parent / "scripts" / "wrapper-candidate-discovery.sh").read_text()
        assert w.index("run-candidate-discovery.sh") < w.index('cp "$REPO_DIR/config/fund_candidates.json" "$GUARD_BEFORE"')
        program = (Path(g.__file__).parent / "candidate-discovery" / "program.md").read_text()
        assert "already ran" in program and "Do not run them again" in program

    def test_agent_promoting_directly_stays_illegal_even_with_stale_fields(self):
        # "promoted" is never a pipeline-script transition (only
        # gmia-trial-manager.py sets it, in a separate process after guard
        # runs) so it's always flagged regardless of other fields.
        before = [_c("a", "discovered")]
        after = [_c("a", "promoted")]
        assert g.find_illegal_changes(before, after) == [
            {"id": "a", "from": "discovered", "to": "promoted"}
        ]

    def test_new_candidate_as_seed_is_allowed(self):
        before = []
        after = [_c("new", "seed")]
        assert g.find_illegal_changes(before, after) == []

    def test_new_candidate_as_promoted_is_illegal(self):
        before = []
        after = [_c("new", "promoted")]
        assert g.find_illegal_changes(before, after) == [
            {"id": "new", "from": None, "to": "promoted"}
        ]

    def test_new_candidate_legitimately_discovered_in_same_session_is_not_flagged(self):
        # Phase 3 of program.md tells the agent to add a bare "seed" candidate,
        # then run `discover_fund_sites.py --fund <new-id>` in the same session.
        # That script legitimately advances seed -> discovered and stamps
        # last_discovered_at, same as it does for pre-existing candidates. The
        # before-snapshot has no entry at all for a brand-new id, so this must
        # be checked against PIPELINE_STATUS_TRANSITIONS too, not just rejected
        # outright (2026-07-29 blue-owl-capital false positive).
        before = []
        after = [{"id": "new", "status": "discovered", "notes": "x",
                  "last_discovered_at": "2026-07-29T09:03:05+08:00"}]
        assert g.find_illegal_changes(before, after) == []

    def test_new_candidate_with_pipeline_status_but_no_stamp_is_illegal(self):
        # Same target status as above, but no freshness stamp present at all
        # — no proof the real script ran, so still a hand-edit forgery.
        before = []
        after = [_c("new", "discovered")]
        assert g.find_illegal_changes(before, after) == [
            {"id": "new", "from": None, "to": "discovered"}
        ]


class TestApplyCorrections:
    def test_reverts_illegal_change_to_prior_status(self):
        before = [_c("a", "discovered")]
        after = [_c("a", "promoted")]
        illegal = g.find_illegal_changes(before, after)
        corrected = g.apply_corrections(after, illegal)
        assert corrected[0]["status"] == "discovered"

    def test_leaves_legal_candidates_untouched(self):
        before = [_c("a", "discovered"), _c("b", "promoted")]
        after = [_c("a", "watchlist"), _c("b", "promoted")]
        illegal = g.find_illegal_changes(before, after)
        corrected = g.apply_corrections(after, illegal)
        assert [c["status"] for c in corrected] == ["watchlist", "promoted"]


class TestStampLegalStatusChanges:
    """The agent edits fund_candidates.json directly (bypassing status_util),
    so its legal changes (watchlist/rejected downgrade, brand-new seed) never
    get a status_since stamp on their own. Guard already has the before/after
    snapshot, so it's the natural place to backfill it — otherwise stall
    detection would think these candidates have been stuck forever."""

    def test_legal_downgrade_gets_stamped(self):
        before = [_c("a", "discovered")]
        after = [_c("a", "watchlist")]
        illegal = g.find_illegal_changes(before, after)
        stamped = g.stamp_legal_status_changes(before, after, illegal)
        assert stamped == ["a"]
        assert after[0]["status_since"] is not None

    def test_new_seed_candidate_gets_stamped(self):
        before = []
        after = [_c("new", "seed")]
        illegal = g.find_illegal_changes(before, after)
        stamped = g.stamp_legal_status_changes(before, after, illegal)
        assert stamped == ["new"]
        assert after[0]["status_since"] is not None

    def test_unchanged_status_not_stamped(self):
        before = [_c("a", "promoted")]
        after = [_c("a", "promoted")]
        illegal = g.find_illegal_changes(before, after)
        stamped = g.stamp_legal_status_changes(before, after, illegal)
        assert stamped == []
        assert "status_since" not in after[0]

    def test_illegal_change_not_stamped_here(self):
        # Reverted separately by apply_corrections — stamping happens on the
        # corrected (reverted) list, where this candidate's status is back to
        # unchanged, so it must not be double-counted as a legal change.
        before = [_c("a", "discovered")]
        after = [_c("a", "promoted")]
        illegal = g.find_illegal_changes(before, after)
        corrected = g.apply_corrections(after, illegal)
        stamped = g.stamp_legal_status_changes(before, corrected, illegal)
        assert stamped == []
