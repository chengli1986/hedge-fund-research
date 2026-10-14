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
    parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    return (datetime.now(BJT) - parsed).days


def tag_note(candidate: dict, marker: str) -> None:
    """Prepend an audit marker to candidate["notes"], truncated to 300 chars
    (matches the on-disk convention used by guard_candidate_status.py)."""
    existing = candidate.get("notes") or ""
    candidate["notes"] = (marker + " " + existing)[:300]


def awaiting_auto_promote(candidates: list[dict], production_ids: set[str]) -> list[str]:
    """Ids the discovery report lists as "Awaiting Auto-Promote".

    The rule wrapper-auto-promote.sh wires by: status "promoted" and not yet
    in config/sources.json. A trial-history "pass" is not enough -- PineBridge
    passed in May, was rejected in July, and kept showing as awaiting.
    """
    return [c["id"] for c in candidates
            if c.get("status") == "promoted" and c["id"] not in production_ids]


def days_since_last_new_candidate(candidates: list[dict], now: datetime | None = None) -> int | None:
    """Whole days since the newest candidate's added_at (BJT date), or None
    when no candidate carries one. The discovery report shows it so a run of
    sessions that never look for new funds is visible (2026-10-14)."""
    dates = [c["added_at"] for c in candidates if c.get("added_at")]
    if not dates:
        return None
    today = (now or datetime.now(BJT)).astimezone(BJT).date()
    return (today - datetime.fromisoformat(max(dates)).date()).days
