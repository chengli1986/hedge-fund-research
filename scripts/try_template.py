#!/usr/bin/env python3
"""Try a listing template on a candidate's live site, and record the result.

The synthesis agent must run this before writing any fetch_<id> (2026-10-13,
"template first"). The record is written by this script, not by the agent:
an agent that summarises its own attempt can report one it never made.

    python3 scripts/try_template.py --candidate rbc-bluebay \
        --spec '{"type": "card_list", "fetch": "requests", "card": "div.card", "link": "a"}' \
        [--listing-url URL] [--record]

Prints the rows and the verdict. With --record it writes
candidate["template_attempts"] (every try, newest last) and, on a pass,
listing_template / listing_url / expected_hostname, which the trial and
auto-promote then use instead of a hand-written fetcher.

Exit: 0 the template is good enough, 1 it is not, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import listing_templates as lt  # noqa: E402

CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"


def _load(path: Path) -> tuple[object, list[dict]]:
    data = json.loads(path.read_text())
    return data, (data if isinstance(data, list) else data.get("candidates", []))


def _save(path: Path, data: object) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def attempt(candidate: dict, spec: dict, listing_url: str | None = None,
            fetch=None) -> dict:
    """Run the spec against the live site; return the attempt record."""
    fetch = fetch or lt.fetch      # looked up at call time, not pinned at import
    trial = dict(candidate, listing_template=spec)
    if listing_url:
        trial["listing_url"] = listing_url
    source = lt.candidate_source(trial)
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "type": spec.get("type"), "spec": spec, "listing_url": source["url"]}
    try:
        lt.validate_spec(spec)
        rows = fetch(source)
    except Exception as exc:
        record.update(ok=False, rows=0, problems=[f"{type(exc).__name__}: {str(exc)[:200]}"])
        return record
    problems = lt.judge_rows(rows, source["expected_hostname"])
    record.update(ok=not problems, rows=len(rows),
                  dated=sum(1 for r in rows if r.get("date")), problems=problems,
                  sample=[{"title": r.get("title"), "url": r.get("url"), "date": r.get("date")}
                          for r in rows[:3]])
    record["_source"] = source
    return record


def record_attempt(path: Path, candidate_id: str, rec: dict) -> None:
    data, candidates = _load(path)
    cand = next((c for c in candidates if c["id"] == candidate_id), None)
    if cand is None:
        raise SystemExit(f"candidate {candidate_id!r} vanished from {path}")
    source = rec.pop("_source", None)
    cand.setdefault("template_attempts", []).append(rec)
    if rec["ok"] and source:
        cand["listing_template"] = rec["spec"]
        cand["listing_url"] = source["url"]
        cand["expected_hostname"] = source["expected_hostname"]
    _save(path, data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--spec", required=True, help="JSON object, or @path to a JSON file")
    ap.add_argument("--listing-url", help="page or feed the spec reads (default: research_url)")
    ap.add_argument("--record", action="store_true", help="write the attempt into fund_candidates.json")
    ap.add_argument("--candidates-file", default=str(CANDIDATES_FILE))
    a = ap.parse_args(argv)

    try:
        raw = Path(a.spec[1:]).read_text() if a.spec.startswith("@") else a.spec
        spec = json.loads(raw)
        if not isinstance(spec, dict):
            raise ValueError("the spec must be a JSON object")
    except (OSError, ValueError) as exc:
        print(f"ERROR: bad --spec: {exc}", file=sys.stderr)
        return 2
    path = Path(a.candidates_file)
    _, candidates = _load(path)
    cand = next((c for c in candidates if c["id"] == a.candidate), None)
    if cand is None:
        print(f"ERROR: no candidate {a.candidate!r} in {path}", file=sys.stderr)
        return 2

    rec = attempt(cand, spec, a.listing_url)
    shown = {k: v for k, v in rec.items() if k != "_source"}
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    print("VERDICT:", "PASS — use the template, write no fetcher" if rec["ok"]
          else "FAIL — " + "; ".join(rec["problems"]))
    if a.record:
        record_attempt(path, a.candidate, rec)
        print(f"recorded in {path}")
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
