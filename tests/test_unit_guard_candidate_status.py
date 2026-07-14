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
        # visitable/inaccessible/screened are owned by specific scripts, not the agent
        before = [_c("a", "discovered")]
        after = [_c("a", "visitable")]
        assert g.find_illegal_changes(before, after) == [
            {"id": "a", "from": "discovered", "to": "visitable"}
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
