#!/usr/bin/env python3
"""Counts of why articles have no body or no summary, by source and label.

Reads data/articles.jsonl (current state: content_failure on failed and
permafail rows, analysis_label on declined rows) and logs/content-failures.jsonl
(every labelled failure event). Labels and their meaning live in
failure_labels.py; the cases behind them in docs/content-failure-casebook.md.

  python3 scripts/failure_stats.py            # current state + last 7 days
  python3 scripts/failure_stats.py --days 30
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
import failure_labels  # noqa: E402

DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
LEDGER_FILE = BASE_DIR / "logs" / "content-failures.jsonl"


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def current_content_failures(rows, configured) -> Counter:
    c = Counter()
    for r in rows:
        if r.get("source_id") in configured and r.get("content_status") in ("failed", "permafail"):
            c[(r["source_id"], (r.get("content_failure") or {}).get("label", "unlabelled"))] += 1
    return c


def current_analysis_declines(rows, configured) -> Counter:
    c = Counter()
    for r in rows:
        if r.get("source_id") in configured and r.get("analysis_status") == "insufficient_content":
            label = r.get("analysis_label") or failure_labels.classify_analysis_decline(r.get("analysis_reason", ""))
            c[(r["source_id"], label)] += 1
    return c


def ledger_summary(path: Path, days: int) -> tuple[Counter, list]:
    """Events in the window by (source, label), and pairs first seen in it."""
    if not path.exists():
        return Counter(), []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    inside, before = Counter(), set()
    for e in _rows(path):
        try:
            at = datetime.fromisoformat(e["at"])
        except (KeyError, ValueError, TypeError):
            continue
        key = (e.get("source_id"), e.get("label"))
        if at >= cutoff:
            inside[key] += 1
        else:
            before.add(key)
    return inside, sorted(k for k in inside if k not in before)


# A (source, label) pair seen for the first time in the window, for one of
# these labels, is worth an email: a redesign, a new block, a PDF that stopped
# matching, or a document landing under the wrong title.
ALERT_CONTENT_LABELS = ("selector_miss", "blocked_by_bot_protection", "pdf_not_usable")
ALERT_DECLINE_LABELS = ("wrong_document", "duplicate_body", "grounding_failed")


def _when(value):
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else None


def recent_intake(rows, configured, days: int) -> dict:
    """What happened to articles first fetched in the last `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = {"total": 0, "with_body": 0, "metadata_only": 0, "without_body": 0, "declined": 0}
    for r in rows:
        at = _when(r.get("fetched_at"))
        if r.get("source_id") not in configured or at is None or at < cutoff:
            continue
        out["total"] += 1
        status = r.get("content_status")
        if status in ("ok", "metadata_only"):
            out["with_body"] += 1
        if status == "metadata_only":
            out["metadata_only"] += 1
        if status in ("failed", "permafail"):
            out["without_body"] += 1
        if r.get("analysis_status") == "insufficient_content":
            out["declined"] += 1
    return out


def analysis_first_seen(rows, configured, days: int, since=None) -> list:
    """(source, decline label) pairs whose first decline is after the cutoff:
    `since` (the previous health run) when given, else the window start."""
    cutoff = _when(since) or datetime.now(timezone.utc) - timedelta(days=days)
    inside, before = set(), set()
    for r in rows:
        if r.get("source_id") not in configured or r.get("analysis_status") != "insufficient_content":
            continue
        at = _when(r.get("analysis_checked_at"))
        if at is None:
            continue
        label = r.get("analysis_label") or failure_labels.classify_analysis_decline(r.get("analysis_reason", ""))
        (inside if at >= cutoff else before).add((r["source_id"], label))
    return sorted(inside - before)


def _ledger_first_seen(path: Path, since) -> list:
    """(source, label) pairs whose earliest ledger event is after `since`."""
    if not path.exists() or _when(since) is None:
        return []
    first: dict = {}
    for e in _rows(path):
        at = _when(e.get("at"))
        if at is None:
            continue
        key = (e.get("source_id"), e.get("label"))
        if key not in first or at < first[key]:
            first[key] = at
    return sorted(k for k, at in first.items() if at > _when(since))


def quality_summary(rows, configured, ledger: Path, days: int = 7, since=None) -> dict:
    """Everything the health email's content-quality section shows.

    Alerts are pairs first seen after `since` (the previous health run), so
    each is reported once; without `since` the window start is the cutoff.
    """
    new_content = _ledger_first_seen(ledger, since) if _when(since) else ledger_summary(ledger, days)[1]
    alerts = [("content", s, l) for s, l in new_content if l in ALERT_CONTENT_LABELS]
    alerts += [("analysis", s, l) for s, l in analysis_first_seen(rows, configured, days, since=since)
               if l in ALERT_DECLINE_LABELS]
    return {"days": days, "intake": recent_intake(rows, configured, days),
            "content": current_content_failures(rows, configured),
            "declines": current_analysis_declines(rows, configured),
            "alerts": alerts}


def _table(counter: Counter, descriptions: dict) -> list[str]:
    if not counter:
        return ["  (none)"]
    by_label = Counter()
    for (_, label), n in counter.items():
        by_label[label] += n
    lines = []
    for label, n in by_label.most_common():
        lines.append(f"  {label:28s} {n:4d}  {descriptions.get(label, '')}")
        for (sid, lab), m in sorted(counter.items(), key=lambda kv: -kv[1]):
            if lab == label:
                lines.append(f"      {sid:30s} {m:4d}")
    return lines


def render_report(rows, configured, ledger: Path, days: int) -> str:
    out = ["Content failures (current state)"]
    out += _table(current_content_failures(rows, configured), failure_labels.CONTENT_FAILURE_LABELS)
    out += ["", "Analysis declines (current state)"]
    out += _table(current_analysis_declines(rows, configured), failure_labels.ANALYSIS_DECLINE_LABELS)
    counts, new = ledger_summary(ledger, days)
    out += ["", f"Last {days} days of content-failure events"]
    out += _table(counts, failure_labels.CONTENT_FAILURE_LABELS)
    if new:
        out += ["  first seen in this window: " + ", ".join(f"{s}/{l}" for s, l in new)]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    configured = {s["id"] for s in json.loads(SOURCES_FILE.read_text())["sources"]}
    print(render_report(_rows(DATA_FILE), configured, LEDGER_FILE, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
