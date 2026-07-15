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

Phase 1 of the same session runs the real pipeline scripts (discover_fund_sites.py
/ screen_fund_candidates.py / discover_candidate_entrypoints.py) via Bash, and
those legitimately set these same statuses through status_util.set_status().
PIPELINE_STATUS_TRANSITIONS lets the guard tell that apart from a hand-edit by
checking whether the script's own freshness field actually advanced.

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

from status_util import now_iso

# Statuses the discovery agent is allowed to set on an EXISTING candidate
# (besides leaving it unchanged).
ALLOWED_AGENT_STATUSES = {"watchlist", "rejected"}
# Status the agent is allowed to give a brand-new candidate.
ALLOWED_NEW_STATUS = "seed"

# Status transitions owned by program.md's Phase 1 pipeline scripts
# (discover_fund_sites.py / screen_fund_candidates.py /
# discover_candidate_entrypoints.py), which the agent runs via Bash in the
# same session this guard's before/after snapshot spans. Each script calls
# status_util.set_status() itself and stamps its own freshness field at the
# same time, so a transition is only treated as legitimate when that field
# actually advanced in this session — proof the real script ran, not the
# agent hand-editing the status field via Edit/Write to match. Without this,
# every ordinary Phase 1 pipeline run gets reverted as if it were the agent
# illegally promoting/rejecting via direct edit (2026-07-15 longleaf-partners/
# lord-abbett/invesco false-positive: screen_fund_candidates.py legitimately
# screened all 3, discover_candidate_entrypoints.py legitimately marked 2
# inaccessible, guard reverted all of it back to "discovered").
PIPELINE_STATUS_TRANSITIONS = {
    "screened": "last_screened_at",
    "screen_failed": "last_screened_at",
    "visitable": "last_validated_at",
    "inaccessible": "last_validated_at",
    "discovered": "last_discovered_at",
}


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
            if to != ALLOWED_NEW_STATUS:
                illegal.append({"id": cid, "from": None, "to": to})
            continue
        frm = prev.get("status")
        if to == frm or to in ALLOWED_AGENT_STATUSES:
            continue
        stamp_field = PIPELINE_STATUS_TRANSITIONS.get(to)
        if stamp_field and c.get(stamp_field) != prev.get(stamp_field):
            continue  # legitimate: proven by a fresh script-owned timestamp
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
        if cid in illegal_ids:
            continue
        prev = before_by_id.get(cid)
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
        # Match the on-disk format written by the rest of the pipeline:
        # json.dumps(indent=2) (ensure_ascii=True default), no trailing newline.
        with open(args.after, "w") as f:
            f.write(json.dumps(corrected, indent=2))
        for x in illegal:
            print(f"REVERTED {x['id']}: {x['from']} -> {x['to']} (reverted)")
        if stamped:
            print(f"STAMPED status_since for {len(stamped)} legal change(s): {', '.join(stamped)}")
    print(f"guard: {len(illegal)} illegal status change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
