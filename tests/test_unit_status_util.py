"""Unit tests for status_util — the shared status_since stamping helper.

status_since must record when a candidate's status LAST actually changed,
so downstream code (stall detection, email visibility) can tell "just added
today" apart from "stuck for two weeks" instead of both looking identical.
"""
from datetime import datetime, timedelta, timezone

import status_util as su

BJT = timezone(timedelta(hours=8))


class TestSetStatus:
    def test_status_change_stamps_status_since(self):
        c = {"id": "a", "status": "seed"}
        changed = su.set_status(c, "discovered")
        assert changed is True
        assert c["status"] == "discovered"
        assert c["status_since"] is not None
        # Round-trips as an ISO timestamp with BJT offset.
        parsed = datetime.fromisoformat(c["status_since"])
        assert parsed.utcoffset() == timedelta(hours=8)

    def test_same_status_does_not_touch_status_since(self):
        c = {"id": "a", "status": "discovered", "status_since": "2026-01-01T00:00:00+08:00"}
        changed = su.set_status(c, "discovered")
        assert changed is False
        assert c["status_since"] == "2026-01-01T00:00:00+08:00"

    def test_first_ever_status_still_stamps(self):
        # No prior status_since field at all (legacy candidate) — first real
        # transition through the helper must still stamp it.
        c = {"id": "a", "status": "seed"}
        su.set_status(c, "seed")  # no-op: status unchanged, no status_since yet
        assert c.get("status_since") is None
        su.set_status(c, "discovered")
        assert c["status_since"] is not None


class TestDaysSince:
    def test_days_since_computes_whole_days(self):
        since = (datetime.now(BJT) - timedelta(days=5, hours=2)).isoformat()
        c = {"status_since": since}
        assert su.days_since(c) == 5

    def test_days_since_missing_field_returns_none(self):
        assert su.days_since({}) is None

    def test_days_since_zero_for_just_now(self):
        c = {"status_since": datetime.now(BJT).isoformat()}
        assert su.days_since(c) == 0


class TestTagNote:
    def test_tag_note_prepends_marker(self):
        c = {"notes": "existing note"}
        su.tag_note(c, "[auto-stall 2026-07-15] seed->discovered after 3d stuck")
        assert c["notes"].startswith("[auto-stall 2026-07-15]")
        assert "existing note" in c["notes"]

    def test_tag_note_handles_missing_notes(self):
        c = {}
        su.tag_note(c, "[auto-stall 2026-07-15] marker")
        assert c["notes"].startswith("[auto-stall 2026-07-15]")

    def test_tag_note_truncates_to_300_chars(self):
        c = {"notes": "x" * 400}
        su.tag_note(c, "[auto-stall 2026-07-15] marker")
        assert len(c["notes"]) <= 300
