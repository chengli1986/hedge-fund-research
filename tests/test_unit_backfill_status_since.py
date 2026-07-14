"""Unit tests for backfill_status_since — one-time backfill for the
status_since field on candidates that existed before it was introduced.

No exact history exists for when each candidate entered its current status,
so this uses the closest available last_*_at field as a fallback proxy
(last_validated_at > last_screened_at > last_discovered_at > last_deep_analyzed_at).
Idempotent: candidates that already have status_since are left untouched, so
it's safe to re-run.
"""
import backfill_status_since as bss


def _c(cid, status, **extra):
    c = {"id": cid, "status": status, "notes": ""}
    c.update(extra)
    return c


class TestBackfillOne:
    def test_already_has_status_since_is_untouched(self):
        c = _c("a", "promoted", status_since="2026-01-01T00:00:00+08:00",
               last_validated_at="2026-06-01T00:00:00+08:00")
        changed = bss.backfill_one(c)
        assert changed is False
        assert c["status_since"] == "2026-01-01T00:00:00+08:00"

    def test_uses_last_validated_at_first(self):
        c = _c("a", "promoted",
               last_validated_at="2026-06-01T00:00:00+08:00",
               last_screened_at="2026-05-01T00:00:00+08:00",
               last_discovered_at="2026-04-01T00:00:00+08:00")
        changed = bss.backfill_one(c)
        assert changed is True
        assert c["status_since"] == "2026-06-01T00:00:00+08:00"

    def test_falls_back_to_last_screened_at(self):
        c = _c("a", "screen_failed", last_screened_at="2026-05-01T00:00:00+08:00")
        bss.backfill_one(c)
        assert c["status_since"] == "2026-05-01T00:00:00+08:00"

    def test_falls_back_to_last_discovered_at(self):
        c = _c("a", "discovered", last_discovered_at="2026-04-01T00:00:00+08:00")
        bss.backfill_one(c)
        assert c["status_since"] == "2026-04-01T00:00:00+08:00"

    def test_falls_back_to_last_deep_analyzed_at(self):
        c = _c("a", "watchlist", last_deep_analyzed_at="2026-03-01T00:00:00Z")
        bss.backfill_one(c)
        assert c["status_since"] == "2026-03-01T00:00:00Z"

    def test_no_fallback_available_leaves_unset(self):
        c = _c("a", "seed")
        changed = bss.backfill_one(c)
        assert changed is False
        assert "status_since" not in c


class TestMainIntegration:
    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        import json
        cand_file = tmp_path / "fund_candidates.json"
        cand_file.write_text(json.dumps([
            _c("a", "promoted", last_validated_at="2026-06-01T00:00:00+08:00"),
        ]))
        monkeypatch.setattr(bss, "CANDIDATES_FILE", cand_file)
        bss.main(["--dry-run"])
        on_disk = json.loads(cand_file.read_text())
        assert "status_since" not in on_disk[0]

    def test_live_run_backfills_and_is_idempotent(self, tmp_path, monkeypatch):
        import json
        cand_file = tmp_path / "fund_candidates.json"
        cand_file.write_text(json.dumps([
            _c("a", "promoted", last_validated_at="2026-06-01T00:00:00+08:00"),
            _c("b", "watchlist", status_since="2026-01-01T00:00:00+08:00"),
        ]))
        monkeypatch.setattr(bss, "CANDIDATES_FILE", cand_file)
        bss.main([])
        on_disk = json.loads(cand_file.read_text())
        by_id = {c["id"]: c for c in on_disk}
        assert by_id["a"]["status_since"] == "2026-06-01T00:00:00+08:00"
        assert by_id["b"]["status_since"] == "2026-01-01T00:00:00+08:00"  # untouched

        # Re-run must be a no-op (idempotent).
        bss.main([])
        on_disk2 = json.loads(cand_file.read_text())
        assert on_disk2 == on_disk
