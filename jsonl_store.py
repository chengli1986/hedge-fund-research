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

import contextlib
import fcntl
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Stage 1 appends; stages 2 and 3 rewrite the file whole. Those two shapes do
# not compose -- an append lands in the old inode while a rewrite swaps in a
# file that never had it, and the appended rows vanish with no error (audit
# A4). The nightly stages run one after another, so what actually risks this is
# a hand-run command overlapping the cron (which happened repeatedly during
# this audit) or scripts/content_audit.py --mark-gone, which rewrites too.
#
# Waiting forever would turn a stuck process into a stuck pipeline, so the wait
# is bounded and then fails loudly: stopping with a clear reason is
# recoverable; hanging until the cron timeout kills the run mid-write is the
# thing this lock exists to prevent.
LOCK_TIMEOUT_SECONDS = 60
_LOCK_POLL_SECONDS = 0.05


@contextlib.contextmanager
def file_lock(path: Path, exclusive: bool = True):
    """flock a sidecar <path>.lock. Raises TimeoutError if it cannot be had.

    The lock lives beside the file, not on it: a rewrite replaces the file by
    rename, so a lock held on the old inode would protect nothing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o664)
    try:
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not lock {lock_path} within {LOCK_TIMEOUT_SECONDS}s -- another "
                        "process is holding it; check for a stuck fetch/analyze/publish run")
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_rows(path: Path, require: str | None = None,
              keep_damaged: list[bytes] | None = None) -> tuple[list[dict], int]:
    """(rows, damaged) from a JSONL file. A missing file reads empty.

    `keep_damaged`, when given, receives every line that could not be read,
    verbatim, so a caller that rewrites the file can carry them over
    (rewrite_rows(..., preserve=)) instead of deleting the evidence: stages 2
    and 3 rewrite the store from the rows they could parse, and without this
    a torn line was gone from disk for good and stage 1 re-ingested the
    article as new (audit F5).

    `require` names a field a row must carry to count as readable: stage 1
    indexes by "id", and a row without one would silently index nothing.
    """
    path = Path(path)
    if not path.exists():
        return [], 0
    with file_lock(path, exclusive=False):
        content = path.read_bytes()
    return _parse_rows(path, content, require, keep_damaged)


def _read_locked(path: Path) -> tuple[list[dict], int]:
    """read_rows for a caller that already holds the lock."""
    return _parse_rows(path, path.read_bytes(), None, None)


def _parse_rows(path: Path, content: bytes, require, keep_damaged) -> tuple[list[dict], int]:
    rows: list[dict] = []
    damaged = 0
    # Decoded line by line: a write torn inside a multi-byte character (rows
    # are written with ensure_ascii=False) must cost that one line, not raise
    # UnicodeDecodeError before the first line is read and lose them all.
    for n, raw in enumerate(content.split(b"\n"), start=1):
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
            if keep_damaged is not None:
                keep_damaged.append(raw)
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
    with file_lock(path):
        _append_locked(path, rows)


def _append_locked(path: Path, rows: list[dict]) -> None:
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


def rewrite_rows(path: Path, rows: list[dict], preserve: list[bytes] | None = None) -> None:
    """Replace the file with `rows`, atomically: temp file, fsync, rename.

    `preserve`: raw lines read_rows could not parse (its `keep_damaged`),
    written back verbatim after the rows so they stay on disk as evidence and
    every later read still reports them."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        rows = _carry_over_unseen(path, rows)
        _rewrite_locked(path, rows, preserve)


def _carry_over_unseen(path: Path, rows: list[dict]) -> list[dict]:
    """`rows` plus any row on disk the caller never saw.

    The lock makes one operation atomic; it does not make a STAGE atomic.
    Stages 2 and 3 read the store, work for minutes, then rewrite it from what
    they read -- so a row appended in between is not in their list and a
    faithful rewrite would drop it (audit A4, over a longer window than the
    lock alone covers). Re-reading inside the lock and carrying over rows whose
    id is absent from the caller's list keeps them. Rows without an id cannot
    be matched, so this does nothing for them and nothing has ever written one.
    """
    if not path.exists():
        return rows
    known = {r.get("id") for r in rows if r.get("id")}
    if not known:
        return rows
    on_disk, _ = _read_locked(path)
    unseen = [r for r in on_disk if r.get("id") and r["id"] not in known]
    if unseen:
        log.warning("CARRIED OVER: %d row(s) were appended while this run was working; "
                    "keeping them rather than overwriting (ids: %s)",
                    len(unseen), ", ".join(str(r["id"]) for r in unseen[:5]))
    return rows + unseen


def _rewrite_locked(path: Path, rows: list[dict], preserve: list[bytes] | None = None) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    data += b"".join(line.rstrip(b"\r\n") + b"\n" for line in (preserve or []))
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
