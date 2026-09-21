"""Two processes writing the article store must not lose each other's rows.

Audit A4. Stage 1 appends to data/articles.jsonl; stages 2 and 3 rewrite it
whole (temp file + rename). Those two shapes do not compose: an append lands
in the old inode while a rewrite swaps in a file that never had it, and the
appended rows are gone with no error anywhere. The nightly stages run one
after another so this does not bite on its own -- what does is a hand-run
command overlapping the cron, which happened repeatedly while this audit was
being done (analyze_articles.py and publish.py were run by hand on 2026-09-16
and 2026-09-17), plus scripts/content_audit.py --mark-gone, which rewrites the
file too.

The fix is an exclusive lock around every write and a shared lock around every
read, taken in jsonl_store because that is the one place that touches the
store. A lock that waits forever would turn a stuck process into a stuck
pipeline, so it waits LOCK_TIMEOUT_SECONDS and then fails loudly.
"""
import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

import jsonl_store


def _appender(path, start, count):
    jsonl_store.append_rows(Path(path), [{"id": f"a{start + i}"} for i in range(count)])


def _rewriter(path, rows):
    time.sleep(0.05)
    existing, _ = jsonl_store.read_rows(Path(path))
    jsonl_store.rewrite_rows(Path(path), existing + [{"id": r} for r in rows])


class TestTheLock:
    def test_two_appenders_keep_every_row(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        procs = [mp.Process(target=_appender, args=(str(f), i * 100, 40)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        rows, damaged = jsonl_store.read_rows(f)
        assert damaged == 0, "a concurrent append tore a line"
        assert len(rows) == 160, f"rows were lost: {len(rows)}"

    def test_a_writer_waits_for_the_holder(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.append_rows(f, [{"id": "first"}])
        with jsonl_store.file_lock(f):
            started = time.monotonic()
            p = mp.Process(target=_appender, args=(str(f), 900, 1))
            p.start()
            time.sleep(0.4)
            assert p.is_alive(), "the second writer did not wait for the lock"
        p.join(30)
        assert time.monotonic() - started >= 0.4
        rows, _ = jsonl_store.read_rows(f)
        assert [r["id"] for r in rows] == ["first", "a900"]

    def test_waiting_forever_is_not_an_option(self, tmp_path, monkeypatch):
        """A stuck holder must not become a stuck pipeline."""
        f = tmp_path / "articles.jsonl"
        monkeypatch.setattr(jsonl_store, "LOCK_TIMEOUT_SECONDS", 0.3)
        holder = mp.Process(target=_hold_lock, args=(str(f), 5))
        holder.start()
        time.sleep(0.4)
        try:
            with pytest.raises(TimeoutError):
                jsonl_store.append_rows(f, [{"id": "blocked"}])
        finally:
            holder.terminate()
            holder.join(10)

    def test_a_read_sees_whole_lines_only(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.append_rows(f, [{"id": f"r{i}"} for i in range(50)])
        p = mp.Process(target=_appender, args=(str(f), 500, 50))
        p.start()
        seen = []
        for _ in range(20):
            rows, damaged = jsonl_store.read_rows(f)
            seen.append(damaged)
        p.join(30)
        assert set(seen) == {0}, "a reader saw a partially written line"

    def test_the_lock_file_does_not_pollute_the_data_directory(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.append_rows(f, [{"id": "x"}])
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["articles.jsonl", "articles.jsonl.lock"], names


def _hold_lock(path, seconds):
    with jsonl_store.file_lock(Path(path)):
        time.sleep(seconds)


def _appender_delayed(path, start, delay):
    time.sleep(delay)
    jsonl_store.append_rows(Path(path), [{"id": f"a{start}"}])


def _appender_slow(path, start, count):
    for i in range(count):
        jsonl_store.append_rows(Path(path), [{"id": f"s{start + i}"}])
        time.sleep(0.02)


class TestReadModifyWrite:
    """The lock makes each operation atomic; it does not make a stage atomic.

    Stages 2 and 3 do `rows = read_rows(...)`, work for minutes, then
    `rewrite_rows(rows)`. Anything appended in between is not in `rows`, so the
    rewrite would drop it even with a perfect lock -- the exact shape audit A4
    describes, just over a longer window. rewrite_rows therefore re-reads the
    file while holding the lock and carries over any row the caller never saw.
    """
    def test_rows_appended_during_a_long_edit_survive_the_rewrite(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.append_rows(f, [{"id": f"old{i}"} for i in range(5)])
        snapshot, _ = jsonl_store.read_rows(f)          # what a stage would load

        p = mp.Process(target=_appender_slow, args=(str(f), 0, 10))
        p.start()
        time.sleep(0.15)                                 # the stage "works" while rows arrive
        for r in snapshot:
            r["summarized"] = True                       # the edit the stage makes
        jsonl_store.rewrite_rows(f, snapshot)
        p.join(30)

        rows, damaged = jsonl_store.read_rows(f)
        ids = [r["id"] for r in rows]
        assert damaged == 0
        assert [r for r in rows if r["id"] == "old0"][0]["summarized"] is True
        assert len([i for i in ids if i.startswith("s")]) == 10, (
            f"rows appended during the edit were dropped: {sorted(ids)}")

    def test_a_rewrite_does_not_duplicate_what_the_caller_already_has(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.append_rows(f, [{"id": "a"}, {"id": "b"}])
        rows, _ = jsonl_store.read_rows(f)
        jsonl_store.rewrite_rows(f, rows)
        again, _ = jsonl_store.read_rows(f)
        assert [r["id"] for r in again] == ["a", "b"]

    def test_a_rewrite_of_rows_without_ids_still_works(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        jsonl_store.rewrite_rows(f, [{"no_id": 1}, {"no_id": 2}])
        rows, _ = jsonl_store.read_rows(f)
        assert len(rows) == 2


def test_the_lock_is_what_makes_the_carry_over_correct(tmp_path, monkeypatch):
    """Carrying over unseen rows closes the long window; the lock closes the
    short one. Re-reading inside the rewrite is still a read followed by a
    write, and an append landing between them is lost unless it has to wait.

    The rewrite is slowed deliberately so that window is wide enough to aim at.
    """
    f = tmp_path / "articles.jsonl"
    jsonl_store.append_rows(f, [{"id": "old"}])
    rows, _ = jsonl_store.read_rows(f)

    real = jsonl_store._carry_over_unseen

    def slow(path, given):
        out = real(path, given)
        time.sleep(0.4)
        return out

    monkeypatch.setattr(jsonl_store, "_carry_over_unseen", slow)
    # The appender waits first, so its write aims at the middle of the slowed
    # rewrite -- after the carry-over read, before the rename.
    p = mp.Process(target=_appender_delayed, args=(str(f), 700, 0.2))
    p.start()
    jsonl_store.rewrite_rows(f, rows)
    p.join(30)

    ids = [r["id"] for r in jsonl_store.read_rows(f)[0]]
    assert "a700" in ids, f"a row appended during the rewrite was overwritten: {ids}"
