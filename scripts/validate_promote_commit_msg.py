#!/usr/bin/env python3
"""Validate that an auto-promote commit message contains the 4 hard-gate
evidence fields required by auto-promote/program.md Phase 3.

Without this, the agent's prompt asks for evidence but nothing actually checks
whether evidence is present. Agent could fabricate "Live test: 10 articles..."
or omit blocks entirely.

Required fields (regex-checked against `git log -1 --format=%B <sha>`):

  1. Live test     — "X articles", "Y chars"; X >= 3, Y >= 500
  2. Haiku quality — "avg=Z" (>= 0.5) and at least one "rel=W" (>= 0.6)
  3. Method        — "Method: <one of: playwright | ssr | rss | api>"
  4. Preview       — quoted preview text >= 50 chars

Usage:
  python3 scripts/validate_promote_commit_msg.py --commit <sha>
  python3 scripts/validate_promote_commit_msg.py --message-file path/to/msg.txt

Exit codes:
  0 = all 4 gates pass
  1 = one or more gates fail (commit must be reverted)
  2 = git failed / commit not found / message unreadable
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Tuple

MIN_ARTICLES = 3
MIN_CONTENT_CHARS = 500
MIN_AVG_QUALITY = 0.5
MIN_RELEVANCE = 0.6
PREVIEW_MIN_CHARS = 50
VALID_METHODS = {"playwright", "ssr", "rss", "api"}


def _read_message_from_git(commit: str) -> str:
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%B", commit],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed: {proc.stderr.strip()}")
    return proc.stdout


def validate_live_test(msg: str) -> Tuple[bool, str]:
    """Check 'Live test: N articles, content M chars' line."""
    m = re.search(
        r"(\d+)\s*articles?.*?(\d+)\s*chars?",
        msg, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return False, "missing 'N articles ... M chars' (Live test line)"

    articles = int(m.group(1))
    chars = int(m.group(2))

    if articles < MIN_ARTICLES:
        return False, f"articles={articles} below threshold ({MIN_ARTICLES})"
    if chars < MIN_CONTENT_CHARS:
        return False, f"content_chars={chars} below threshold ({MIN_CONTENT_CHARS})"
    return True, f"articles={articles}, chars={chars}"


def validate_haiku_quality(msg: str) -> Tuple[bool, str]:
    """Check 'avg=X.XX' >= 0.5 and at least one 'rel=Y.Y' >= 0.6."""
    avg_match = re.search(r"avg\s*=\s*([\d.]+)", msg, re.IGNORECASE)
    if not avg_match:
        return False, "missing avg=X (Haiku quality)"
    try:
        avg = float(avg_match.group(1))
    except ValueError:
        return False, f"avg value not a number: {avg_match.group(1)}"
    if avg < MIN_AVG_QUALITY:
        return False, f"avg={avg} below threshold ({MIN_AVG_QUALITY})"

    # Accept "rel=0.9" or "rel/dep/ext = 0.95/0.80/0.85" (agent may vary format)
    rel_matches = re.findall(r"rel\s*(?:/\S+)?\s*=\s*([\d.]+)", msg, re.IGNORECASE)
    if not rel_matches:
        return False, "missing rel=X (per-article relevance)"

    rels = []
    for r in rel_matches:
        try:
            rels.append(float(r))
        except ValueError:
            continue

    if not any(r >= MIN_RELEVANCE for r in rels):
        return False, f"no article with rel>={MIN_RELEVANCE} (got {rels})"

    return True, f"avg={avg}, max_rel={max(rels):.2f}"


def validate_method(msg: str) -> Tuple[bool, str]:
    """Check 'Method: <playwright|ssr|rss|api>' line."""
    m = re.search(r"Method\s*:\s*(\w+)", msg, re.IGNORECASE)
    if not m:
        return False, "missing 'Method: <type>' line"
    method = m.group(1).lower()
    if method not in VALID_METHODS:
        return False, f"method='{method}' not in {sorted(VALID_METHODS)}"
    return True, f"method={method}"


def validate_preview(msg: str) -> Tuple[bool, str]:
    """Check quoted Preview line with >= 50 chars of body text."""
    # Accept Preview: "..." on one line, or Preview (...): \n "..." on next line.
    # Use single-quoted pattern to avoid escaping double-quotes in the regex.
    m = re.search(
        r'Preview[^:]*:\s*["“”]([^"“”]+)["“”]',
        msg,
        re.DOTALL,
    )
    if not m:
        return False, 'missing \'Preview: "..."\' line'
    preview = m.group(1).strip()
    if len(preview) < PREVIEW_MIN_CHARS:
        return False, f"preview only {len(preview)} chars (need {PREVIEW_MIN_CHARS})"
    return True, f"preview {len(preview)} chars"


CHECKS = (
    ("live_test", validate_live_test),
    ("haiku_quality", validate_haiku_quality),
    ("method", validate_method),
    ("preview", validate_preview),
)


def validate_message(msg: str) -> dict:
    """Run all 4 hard-gate checks. Returns {ok, results: [(name, passed, detail)]}."""
    results = []
    all_ok = True
    for name, fn in CHECKS:
        passed, detail = fn(msg)
        results.append({"check": name, "passed": passed, "detail": detail})
        if not passed:
            all_ok = False
    return {"ok": all_ok, "checks": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--commit", help="git commit sha")
    src.add_argument("--message-file", help="path to file containing the commit message")
    parser.add_argument("--quiet", action="store_true", help="only print summary line")
    args = parser.parse_args()

    if args.commit:
        try:
            msg = _read_message_from_git(args.commit)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            with open(args.message_file, encoding="utf-8") as f:
                msg = f.read()
        except OSError as exc:
            print(f"ERROR: cannot read {args.message_file}: {exc}", file=sys.stderr)
            return 2

    result = validate_message(msg)
    label = (args.commit or args.message_file)[:12]

    if args.quiet:
        status = "OK" if result["ok"] else "FAIL"
        failed = [c["check"] for c in result["checks"] if not c["passed"]]
        print(f"[{status}] {label}" + (f" — failed: {', '.join(failed)}" if failed else ""))
    else:
        for c in result["checks"]:
            mark = "✓" if c["passed"] else "✗"
            print(f"  {mark} {c['check']:14s} {c['detail']}")
        if result["ok"]:
            print(f"OK: commit {label} passes all 4 hard gates")
        else:
            failed = [c["check"] for c in result["checks"] if not c["passed"]]
            print(f"FAIL: commit {label} — failed: {', '.join(failed)}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
