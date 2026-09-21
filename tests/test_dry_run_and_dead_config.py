"""Two things that looked live and were not (audit B10 and B11).

B10 -- `--dry-run` is how a source gets inspected, and 2026-09-16 stopped it
writing config/inspection_state.json. But main() then read that file back to
decide what to print: the ANOMALY lines, and the numbers the TOTAL FETCH
OUTAGE check adds up. So an operator inspecting a source was shown the LAST
REAL RUN's verdict while this run's result was discarded -- and the same
staleness hit a real run whenever the metrics write failed (it is swallowed
with a log.warning), leaving yesterday's non-zero count in place.

B11 -- config/sources.json carried a settings block:
    max_articles_per_source: 10
    article_lookback_days: 90
    data_dir / log_dir
Nothing in the repo reads any of them; the real per-source limit is each
source's own max_articles. A value that looks configurable and is not is worse
than no value at all: the natural way to fetch fewer articles is to edit that
10, and editing it does nothing at all.
"""
import json
from pathlib import Path

import pytest

import fetch_articles as fa

REPO = Path(__file__).resolve().parent.parent
SRC = {"id": "gsam", "name": "GSAM", "short_name": "GSAM", "method": "api",
       "url": "https://x/", "expected_hostname": ""}


class TestDryRunReportsThisRun:
    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        f = tmp_path / "inspection_state.json"
        f.write_text(json.dumps({"gsam": {"last_article_count": 10, "consecutive_zero_count": 0,
                                          "last_inspected_at": "2026-09-20T00:00:00+00:00"}}))
        monkeypatch.setattr(fa, "INSPECTION_STATE_FILE", f)
        monkeypatch.setattr(fa, "CONFIG_FILE", tmp_path / "sources.json")
        (tmp_path / "sources.json").write_text(json.dumps({"sources": [SRC]}))
        monkeypatch.setattr(fa, "DATA_FILE", tmp_path / "articles.jsonl")
        monkeypatch.setattr(fa, "load_entrypoints", lambda: {})
        monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
        return f

    def _run(self, monkeypatch, listing, dry=True):
        import sys
        monkeypatch.setitem(fa.FETCHERS, "gsam", lambda s: listing)
        argv = ["fetch_articles.py"] + (["--dry-run"] if dry else [])
        monkeypatch.setattr(sys, "argv", argv)
        return fa.main()

    def test_a_dry_run_that_finds_nothing_is_reported_as_an_outage(self, state, monkeypatch):
        """The file says 10 found yesterday. This run found none."""
        rc = self._run(monkeypatch, [])
        assert rc == 1, "the outage check read yesterday's count instead of this run's"

    def test_a_dry_run_that_finds_articles_is_not_an_outage(self, state, monkeypatch):
        state.write_text(json.dumps({"gsam": {"last_article_count": 0, "consecutive_zero_count": 3,
                                              "last_inspected_at": "2026-09-20T00:00:00+00:00"}}))
        rc = self._run(monkeypatch, [{"title": "T", "url": "https://x/a", "date": "2026-09-21"}])
        assert rc == 0, "this run found an article; yesterday's zero streak is not this run's news"

    def test_a_dry_run_still_writes_nothing(self, state, monkeypatch):
        before = state.read_text()
        self._run(monkeypatch, [{"title": "T", "url": "https://x/a", "date": "2026-09-21"}])
        assert state.read_text() == before

    def test_a_dry_run_does_not_even_write_the_corrupt_file_evidence(self, state, monkeypatch):
        state.write_text('{"gsam": {"cons')                    # unreadable
        self._run(monkeypatch, [{"title": "T", "url": "https://x/a", "date": "2026-09-21"}])
        assert not list(state.parent.glob("*.corrupt-*")), "a dry run wrote a backup file"

    def test_a_real_run_records_and_reads_the_same_numbers(self, state, monkeypatch):
        self._run(monkeypatch, [{"title": "T", "url": "https://x/a", "date": "2026-09-21"}], dry=False)
        assert json.loads(state.read_text())["gsam"]["last_article_count"] == 1
        assert fa.run_metrics()["gsam"]["last_article_count"] == 1

    def test_the_numbers_survive_a_failed_state_write(self, state, monkeypatch):
        """The write is swallowed on purpose (instrumentation must not kill the
        run); the outage check must still see THIS run's numbers."""
        import pathlib
        real_replace = fa.os.replace
        monkeypatch.setattr(fa.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        rc = self._run(monkeypatch, [], dry=False)
        monkeypatch.setattr(fa.os, "replace", real_replace)
        assert rc == 1, "the outage check fell back to the file after the write failed"


class TestNoDeadConfig:
    def test_every_settings_key_is_read_by_the_code(self):
        settings = json.loads((REPO / "config" / "sources.json").read_text()).get("settings", {})
        sources = [p.read_text(encoding="utf-8") for p in REPO.glob("*.py")]
        sources += [p.read_text(encoding="utf-8") for p in (REPO / "scripts").glob("*.py")]
        dead = [k for k in settings if not any(k in text for text in sources)]
        assert not dead, (
            f"config/sources.json declares settings nothing reads: {dead}. A value that looks "
            "configurable and is not is worse than no value: editing it changes nothing and the "
            "person who edited it believes it did.")
