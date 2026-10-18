"""Shared status_since stamping helper for config/fund_candidates.json.

Every pipeline stage that flips a candidate's status should go through
set_status() instead of assigning c["status"] directly, so status_since only
moves when the status genuinely changes — never on a same-status re-write
(the bug that made last_discovered_at churn daily and hide real stalls).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))


def write_json_atomic(path, obj) -> None:
    """Write obj as indented JSON via a temp file + os.replace, keeping the
    file's mode. A kill mid-write leaves the old file, never half of one
    (stage 6 minor, 2026-10-18: trial manager, guard and the synthesis
    writers wrote fund_candidates.json / trial-state.json in place)."""
    path = Path(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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
    # The agent can write added_at too; a value that is not an ISO date is
    # skipped, not allowed to crash the report (review 2026-10-15).
    dates = []
    for c in candidates:
        try:
            dates.append(datetime.fromisoformat(str(c.get("added_at"))[:10]).date())
        except ValueError:
            continue
    if not dates:
        return None
    today = (now or datetime.now(BJT)).astimezone(BJT).date()
    return (today - max(dates)).days
