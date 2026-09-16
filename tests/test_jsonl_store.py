"""The article store must not lose a row without saying so (audit B8).

data/articles.jsonl was appended to with a plain `open("a")` -- no fsync, and
no check that the file already ended in a newline. A process killed mid-write
(the cron wrapper sends SIGTERM at its timeout, and since 5fe6c88 stage 1
appends once per source rather than once per night, so there are up to 42 of
these windows a night instead of one) leaves a partial line with no
terminator. The next append then concatenates onto it, producing one line that
is neither record.

Both readers -- fetch_articles.load_existing_ids / load_existing_rows,
fetch_content.load_articles, analyze_articles.load_articles -- `continue` past
a JSONDecodeError. So the torn row AND the good row welded to it both vanish
from the id index, from stages 2 and 3 and from the dashboard, with no log
line anywhere. The good row is re-fetched on a later night, so a torn write
turns silently into a duplicate.

jsonl_store is one place that knows how to read and write the store, so the
three stages cannot drift apart on it.
"""
import json
import os

import pytest

import jsonl_store


def _rows(*ids):
    return [{"id": i, "title": f"T{i}"} for i in ids]


class TestAppend:
    def test_a_file_with_no_trailing_newline_is_repaired_first(self, tmp_path):
        """The torn line stays one damaged line instead of eating the next row."""
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1", "title": "Alp')                 # killed mid-write
        jsonl_store.append_rows(f, _rows("a2"))
        lines = f.read_text().splitlines()
        assert len(lines) == 2, lines
        assert json.loads(lines[1])["id"] == "a2"

    def test_the_row_after_a_torn_line_is_readable(self, tmp_path):
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1", "title": "Alp')
        jsonl_store.append_rows(f, _rows("a2"))
        rows, damaged = jsonl_store.read_rows(f)
        assert [r["id"] for r in rows] == ["a2"]
        assert damaged == 1

    def test_an_ordinary_append_adds_one_line_per_row(self, tmp_path):
        f = tmp_path / "a.jsonl"
        jsonl_store.append_rows(f, _rows("a1", "a2"))
        jsonl_store.append_rows(f, _rows("a3"))
        assert [r["id"] for r in jsonl_store.read_rows(f)[0]] == ["a1", "a2", "a3"]

    def test_the_write_reaches_the_disk(self, tmp_path, monkeypatch):
        """Without fsync the rows live in the page cache; a host crash loses
        them while the process reported success."""
        synced = []
        monkeypatch.setattr(jsonl_store.os, "fsync", lambda fd: synced.append(fd))
        jsonl_store.append_rows(tmp_path / "a.jsonl", _rows("a1"))
        assert synced, "append did not fsync"

    def test_appending_nothing_does_not_create_a_file(self, tmp_path):
        f = tmp_path / "a.jsonl"
        jsonl_store.append_rows(f, [])
        assert not f.exists()

    def test_unicode_is_stored_readably(self, tmp_path):
        f = tmp_path / "a.jsonl"
        jsonl_store.append_rows(f, [{"id": "a1", "title": "中文标题"}])
        assert "中文标题" in f.read_text(encoding="utf-8")


class TestRead:
    def test_a_damaged_line_is_counted_and_named(self, tmp_path, caplog):
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1"}\nnot json at all\n{"id": "a3"}\n')
        import logging
        with caplog.at_level(logging.ERROR):
            rows, damaged = jsonl_store.read_rows(f)
        assert [r["id"] for r in rows] == ["a1", "a3"]
        assert damaged == 1
        assert any("line 2" in r.getMessage() for r in caplog.records), \
            "a damaged row must say which line it was on"

    def test_a_row_without_an_id_is_damaged_too(self, tmp_path):
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1"}\n{"title": "no id"}\n')
        rows, damaged = jsonl_store.read_rows(f, require="id")
        assert [r["id"] for r in rows] == ["a1"] and damaged == 1

    def test_a_missing_file_reads_empty(self, tmp_path):
        assert jsonl_store.read_rows(tmp_path / "nope.jsonl") == ([], 0)

    def test_blank_lines_are_not_damage(self, tmp_path):
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1"}\n\n{"id": "a2"}\n')
        rows, damaged = jsonl_store.read_rows(f)
        assert len(rows) == 2 and damaged == 0


class TestRewrite:
    def test_a_rewrite_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        f = tmp_path / "a.jsonl"
        jsonl_store.append_rows(f, _rows("a1"))
        jsonl_store.rewrite_rows(f, _rows("a1", "a2"))
        assert [r["id"] for r in jsonl_store.read_rows(f)[0]] == ["a1", "a2"]
        assert not list(tmp_path.glob("*.tmp*"))

    def test_a_failed_rewrite_leaves_the_old_file_intact(self, tmp_path, monkeypatch):
        f = tmp_path / "a.jsonl"
        jsonl_store.append_rows(f, _rows("a1"))
        monkeypatch.setattr(jsonl_store.os, "replace",
                            lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            jsonl_store.rewrite_rows(f, _rows("a1", "a2"))
        assert [r["id"] for r in jsonl_store.read_rows(f)[0]] == ["a1"]
        assert not list(tmp_path.glob("*.tmp*"))


class TestEveryStageUsesIt:
    """One abstraction, or the three stages drift apart again."""
    def test_stage_1_reads_through_the_store(self, tmp_path, monkeypatch):
        import fetch_articles as fa
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1", "source_id": "aqr", "url": "u", "title": "T", "date": "2026-09-01"}\n'
                     'torn{"id": "a2"}\n')
        monkeypatch.setattr(fa, "DATA_FILE", f)
        assert fa.load_existing_ids() == {"a1"}
        assert [r["id"] for r in fa.load_existing_rows()] == ["a1"]

    def test_stage_1_appends_through_the_store(self, tmp_path, monkeypatch):
        import fetch_articles as fa
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1", "title": "Alp')
        monkeypatch.setattr(fa, "DATA_FILE", f)
        fa.save_articles(_rows("a2"))
        assert len(f.read_text().splitlines()) == 2

    def test_stage_2_and_3_read_through_the_store(self, tmp_path, monkeypatch):
        import fetch_content as fc
        import analyze_articles as aa
        f = tmp_path / "a.jsonl"
        f.write_text('{"id": "a1"}\ntorn line\n{"id": "a3"}\n')
        monkeypatch.setattr(fc, "DATA_FILE", f)
        monkeypatch.setattr(aa, "DATA_FILE", f)
        assert [r["id"] for r in fc.load_articles()] == ["a1", "a3"]
        assert [r["id"] for r in aa.load_articles()] == ["a1", "a3"]
