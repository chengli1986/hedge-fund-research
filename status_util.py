"""Shared status_since stamping helper for config/fund_candidates.json.

Every pipeline stage that flips a candidate's status should go through
set_status() instead of assigning c["status"] directly, so status_since only
moves when the status genuinely changes — never on a same-status re-write
(the bug that made last_discovered_at churn daily and hide real stalls).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(BJT).isoformat()


def set_status(candidate: dict, new_status: str) -> bool:
    """Set candidate["status"], stamping status_since only on an actual change.

    Returns True if the status changed (and status_since was stamped).
    """
    if candidate.get("status") == new_status:
        return False
    candidate["status"] = new_status
    candidate["status_since"] = now_iso()
    return True


def days_since(candidate: dict) -> int | None:
    """Whole days elapsed since candidate["status_since"], or None if unset."""
    since = candidate.get("status_since")
    if not since:
        return None
    parsed = datetime.fromisoformat(since)
    return (datetime.now(BJT) - parsed).days


def tag_note(candidate: dict, marker: str) -> None:
    """Prepend an audit marker to candidate["notes"], truncated to 300 chars
    (matches the on-disk convention used by guard_candidate_status.py)."""
    existing = candidate.get("notes") or ""
    candidate["notes"] = (marker + " " + existing)[:300]
