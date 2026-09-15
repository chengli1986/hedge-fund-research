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
