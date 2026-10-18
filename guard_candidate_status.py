#!/usr/bin/env python3
"""Backstop guard: revert illegal candidate status changes made by the
candidate-discovery agent.

The discovery agent (candidate-discovery/program.md) edits config/fund_candidates.json
to record its analysis. It is ONLY permitted to:
  - leave a candidate's status unchanged, OR
  - downgrade an unsuitable candidate to "watchlist" or "rejected", OR
  - add a brand-new candidate as "seed".

It must NEVER hand-edit a candidate straight to "promoted" (or any other
pipeline-owned status like visitable/inaccessible/screened). Promotion is
gmia-trial-manager's job, and only after a trial PASS — that path also sets
synthesis_priority so the fetcher gets synthesized. An agent that promotes
directly orphans the candidate (no trial, no synthesis_priority, never
queued). See 2026-06-10 Guggenheim incident.

Phase 1's pipeline scripts (discover_fund_sites.py / screen_fund_candidates.py /
discover_candidate_entrypoints.py) set these statuses legitimately; since
2026-10-18 the wrapper runs them before this guard's snapshot, so they are
never inside the before/after window (see NEW_CANDIDATE_TRANSITIONS).

Usage (run after the discovery agent, before trial-manager):
    python3 guard_candidate_status.py --before <snapshot.json> --after config/fund_candidates.json
Reverts any illegal change in --after in place, prints one REVERTED line per fix,
and exits 0. Wrapper inspects stdout / the "guard: N" line to decide whether to
commit the correction and send an alert.
"""
from __future__ import annotations

import argparse
import json
import sys

from status_util import now_iso, write_json_atomic

# Statuses the discovery agent is allowed to set on an EXISTING candidate
# (besides leaving it unchanged).
ALLOWED_AGENT_STATUSES = {"watchlist", "rejected"}
# Status the agent is allowed to give a brand-new candidate.
ALLOWED_NEW_STATUS = "seed"

# Status a brand-new candidate may reach in the session: Phase 3 adds a bare
# seed and runs `discover_fund_sites.py --fund <new-id>`, which advances it to
# "discovered" and stamps last_discovered_at (2026-07-29 blue-owl-capital).
#
# Existing candidates get no such exemption any more (stage 6 #9,
# 2026-10-18). The Phase 1 scripts used to run inside the agent's session,
# so a status they set was told apart from a hand-edit by "did the script's
# own timestamp advance?" -- a field in the same file the agent edits, which
# it could set together with the status. The wrapper now runs Phase 1 before
# this guard's snapshot; inside the session nothing but the agent changes an
# existing candidate, and the agent may only set watchlist or rejected.
NEW_CANDIDATE_TRANSITIONS = {"discovered": "last_discovered_at"}


def find_illegal_changes(before: list[dict], after: list[dict]) -> list[dict]:
    """Return [{id, from, to}] for candidates whose status the agent set illegally.

    Only flags changes introduced between `before` and `after`, so pre-existing
    legitimately-promoted candidates the agent did not touch are never flagged.
    """
    before_by_id = {c.get("id"): c for c in before}
    illegal: list[dict] = []
    for c in after:
        cid = c.get("id")
        to = c.get("status")
        prev = before_by_id.get(cid)
        if prev is None:
            if to == ALLOWED_NEW_STATUS:
                continue
            stamp_field = NEW_CANDIDATE_TRANSITIONS.get(to)
            if stamp_field and c.get(stamp_field):
                continue
            illegal.append({"id": cid, "from": None, "to": to})
            continue
        frm = prev.get("status")
        if to == frm or to in ALLOWED_AGENT_STATUSES:
            continue
        illegal.append({"id": cid, "from": frm, "to": to})
    return illegal


def apply_corrections(after: list[dict], illegal: list[dict]) -> list[dict]:
    """Return a copy of `after` with each illegal status reverted (existing →
    prior status; new candidate → 'seed'), tagging notes for traceability."""
    bad = {x["id"]: x for x in illegal}
    out = []
    for c in after:
        c = dict(c)
        x = bad.get(c.get("id"))
        if x is not None:
            reverted_to = x["from"] if x["from"] is not None else ALLOWED_NEW_STATUS
            c["status"] = reverted_to
            marker = f"[guard: reverted illegal {x['to']}->{reverted_to}; agent may not set this status] "
            c["notes"] = (marker + (c.get("notes") or ""))[:300]
        out.append(c)
    return out


def stamp_legal_status_changes(before: list[dict], after: list[dict], illegal: list[dict]) -> list[str]:
    """Stamp status_since on candidates whose status legally changed (agent
    downgrade to watchlist/rejected, or a brand-new seed candidate).

    The agent edits fund_candidates.json directly, bypassing status_util's
    set_status(), so these changes never get a status_since stamp on their
    own — stall detection would then see a missing/stale status_since and
    either skip the candidate or misjudge how long it's been stuck. Guard
    already holds the before/after snapshot needed to tell "just changed"
    apart from "unchanged since before this run", so it's the natural place
    to backfill it. Mutates `after` in place; returns the stamped ids.
    """
    illegal_ids = {x["id"] for x in illegal}
    before_by_id = {c.get("id"): c for c in before}
    stamped: list[str] = []
    for c in after:
        cid = c.get("id")
        prev = before_by_id.get(cid)
        # A reverted existing candidate is back at its old status: nothing to
        # stamp. A reverted NEW one is a new seed like any other and needs both
        # stamps -- skipping it left it without status_since, so stall
        # detection never routed it, and without added_at (review 2026-10-15).
        if cid in illegal_ids and prev is not None:
            continue
        if prev is None and not c.get("added_at"):
            # The report's "days since the last new candidate" reads this;
            # stamped here so it does not depend on the agent remembering.
            c["added_at"] = now_iso()[:10]
        if prev is None or prev.get("status") != c.get("status"):
            c["status_since"] = now_iso()
            stamped.append(cid)
    return stamped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--before", required=True, help="status snapshot taken before the agent ran")
    ap.add_argument("--after", required=True, help="live candidates file the agent may have edited")
    args = ap.parse_args()

    with open(args.before) as f:
        before = json.load(f)
    with open(args.after) as f:
        after = json.load(f)

    illegal = find_illegal_changes(before, after)
    corrected = apply_corrections(after, illegal) if illegal else after
    stamped = stamp_legal_status_changes(before, corrected, illegal)

    if illegal or stamped:
        # Atomic, and the format every other writer uses (indent=2, UTF-8
        # as is, trailing newline) -- it used to escape non-ASCII, so each
        # writer re-encoded the others' rows (stage 6 minor, 2026-10-18).
        write_json_atomic(args.after, corrected)
        for x in illegal:
            print(f"REVERTED {x['id']}: {x['from']} -> {x['to']} (reverted)")
        if stamped:
            print(f"STAMPED status_since for {len(stamped)} legal change(s): {', '.join(stamped)}")
    print(f"guard: {len(illegal)} illegal status change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
