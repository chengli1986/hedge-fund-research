#!/usr/bin/env python3
"""One place that knows how to read and write the article store.

data/articles.jsonl is appended to by stage 1 and rewritten whole by stages 2
and 3. Before 2026-09-16 each stage carried its own copy of the read loop and
its own writer, and both halves lost rows in silence (audit finding B8):

  · the append used a plain open("a") with no fsync and no check that the file
    already ended in a newline. A process killed mid-write -- the cron wrapper
    SIGTERMs at its timeout, and since 5fe6c88 stage 1 appends once per source
    rather than once per night -- leaves a partial line with no terminator, and
    the next append welds the next row onto it;
  · every reader did `except json.JSONDecodeError: continue`, so the torn row
    AND the good row welded to it both disappeared from the id index, from
    stages 2 and 3 and from the dashboard, with no log line anywhere. The good
    row was re-fetched later, so a torn write turned quietly into a duplicate.

So: appends repair a missing terminator first (which keeps damage to the one
line that was torn), every write is flushed and fsynced, and a line that will
not parse is reported with its line number and counted, never skipped in
silence.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def read_rows(path: Path, require: str | None = None) -> tuple[list[dict], int]:
    """(rows, damaged) from a JSONL file. A missing file reads empty.

    `require` names a field a row must carry to count as readable: stage 1
    indexes by "id", and a row without one would silently index nothing.
    """
    path = Path(path)
    if not path.exists():
        return [], 0
    rows: list[dict] = []
    damaged = 0
    # Decoded line by line: a write torn inside a multi-byte character (rows
    # are written with ensure_ascii=False) must cost that one line, not raise
    # UnicodeDecodeError before the first line is read and lose them all.
    for n, raw in enumerate(path.read_bytes().split(b"\n"), start=1):
        if not raw.strip():
            continue                      # a blank line is formatting, not damage
        line = raw.decode("utf-8", errors="replace")
        try:
            row = json.loads(raw.decode("utf-8"))
            if require is not None and require not in row:
                raise KeyError(require)
            rows.append(row)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            damaged += 1
            log.error("DAMAGED ROW: %s line %d is not a usable record (%s): %.120s",
                      path.name, n, type(exc).__name__, line)
    if damaged:
        log.error("DAMAGED ROWS: %d unusable line(s) in %s -- rows are missing from this run",
                  damaged, path)
    return rows, damaged


def append_rows(path: Path, rows: list[dict]) -> None:
    """Append rows, repairing a missing final newline first, then fsync."""
    rows = list(rows or [])
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                # A row torn by a kill. Terminating it keeps the damage to that
                # one line instead of welding the next row onto it.
                log.error("DAMAGED ROW: %s did not end in a newline -- a previous write was "
                          "interrupted; terminating it so this append stays separate", path.name)
                prefix = "\n"
    data = prefix + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    with path.open("a", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def rewrite_rows(path: Path, rows: list[dict]) -> None:
    """Replace the file with `rows`, atomically: temp file, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
