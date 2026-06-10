#!/usr/bin/env python3
"""Backstop guard: revert illegal candidate status changes made by the
candidate-discovery agent.

The discovery agent (candidate-discovery/program.md) edits config/fund_candidates.json
to record its analysis. It is ONLY permitted to:
  - leave a candidate's status unchanged, OR
  - downgrade an unsuitable candidate to "watchlist" or "rejected", OR
  - add a brand-new candidate as "seed".

It must NEVER set "promoted" (or any other pipeline-owned status like
visitable/inaccessible/screened). Promotion is gmia-trial-manager's job, and only
after a trial PASS — that path also sets synthesis_priority so the fetcher gets
synthesized. An agent that promotes directly orphans the candidate (no trial, no
synthesis_priority, never queued). See 2026-06-10 Guggenheim incident.

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

# Statuses the discovery agent is allowed to set on an EXISTING candidate
# (besides leaving it unchanged).
ALLOWED_AGENT_STATUSES = {"watchlist", "rejected"}
# Status the agent is allowed to give a brand-new candidate.
ALLOWED_NEW_STATUS = "seed"


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
        else:
            frm = prev.get("status")
            if to != frm and to not in ALLOWED_AGENT_STATUSES:
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
    if illegal:
        corrected = apply_corrections(after, illegal)
        # Match the on-disk format written by the rest of the pipeline:
        # json.dumps(indent=2) (ensure_ascii=True default), no trailing newline.
        with open(args.after, "w") as f:
            f.write(json.dumps(corrected, indent=2))
        for x in illegal:
            print(f"REVERTED {x['id']}: {x['from']} -> {x['to']} (reverted)")
    print(f"guard: {len(illegal)} illegal status change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
