#!/usr/bin/env python3
"""Check today's auto-promotions after the agent; revert the ones that fail.

    python3 scripts/verify_promotions.py --phase validate   # commit-message evidence
    python3 scripts/verify_promotions.py --phase probe      # post-commit health probe

Replaces two near-identical heredocs in wrapper-auto-promote.sh (stage-6
audit, 2026-10-14), which:
  - always exited 0, so a revert never failed the job and nothing mailed;
  - left a conflicting `git revert` half-done (REVERT_HEAD, conflict markers
    in config/sources.json for the next cron job to commit);
  - probed, and reverted again, a row the first phase had already reverted;
  - skipped the history line when the push failed, so the local revert was
    invisible to the next phase and to tomorrow's run.

Exit 0: every promotion of the day stands. Exit 1: something was reverted,
or could not be checked or reverted -- the wrapper fails the job, and
cron-wrapper mails. Newest promotion first, so reverts unwind in order.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from promote_today import (BJT, MIN_ARTICLES, MIN_CONTENT_CHARS,  # noqa: E402
                           REVERT_OUTCOMES, live_promotions, today_bjt)
from status_util import set_status, tag_note, write_json_atomic  # noqa: E402

LOG = Path("logs/auto-promote-history.jsonl")
CANDIDATES = Path("config/fund_candidates.json")
# A reverted promotion left the candidate "promoted", so the next night wired
# it again -- every night while the problem lasted (stage 6 minor,
# 2026-10-18). One retry is kept (research-affiliates was reverted 05-14 and
# wired fine 05-15); the second revert within PARK_WINDOW_DAYS parks it.
PARK_AFTER_REVERTS = 2
PARK_WINDOW_DAYS = 7
VALIDATOR = "scripts/validate_promote_commit_msg.py"
HEALTH = "scripts/gmia-fetcher-health.py"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _record(e: dict, outcome: str, notes: str, commit: str = "") -> None:
    row = {"date": e["date"], "timestamp": datetime.now(BJT).isoformat(),
           "id": e["id"], "name": e.get("name", e["id"]), "outcome": outcome,
           "notes": notes, "commit": commit, "reverted_commit": e.get("commit", "")}
    with LOG.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def recent_reverts(sid: str, today: str, days: int = PARK_WINDOW_DAYS) -> int:
    """Reverts of sid recorded in the last `days` days (today included)."""
    if not LOG.exists():
        return 0
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    n = 0
    for line in LOG.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(row, dict) and row.get("id") == sid
                and row.get("outcome") in REVERT_OUTCOMES and start <= str(row.get("date")) <= today):
            n += 1
    return n


def park(sid: str, why: str, tag: str) -> str | None:
    """promoted -> watchlist with a note, committed. Returns a problem line if
    that commit failed, else None (also when there is nothing to park)."""
    cands = json.loads(CANDIDATES.read_text())
    rows = cands if isinstance(cands, list) else cands.get("candidates", [])
    c = next((x for x in rows if x.get("id") == sid), None)
    if c is None or c.get("status") != "promoted":
        return None
    # Someone else's uncommitted edit in the file: committing would sweep it into
    # the park commit, and the failure path's checkout would wipe it (2026-10-21
    # review). Leave the file alone; exit 1 (dirty) and 128 (git error) both stop.
    dirty = _git("diff", "--quiet", "HEAD", "--", str(CANDIDATES))
    if dirty.returncode != 0:
        print(f"[{tag}]   not parking {sid}: {CANDIDATES} has uncommitted changes")
        return (f"{sid}: reverted, but not parked on the watchlist -- {CANDIDATES} has uncommitted "
                f"changes (left untouched; check them); tried again on its next revert")
    set_status(c, "watchlist")
    tag_note(c, f"[auto-promote reverted {PARK_AFTER_REVERTS}x in {PARK_WINDOW_DAYS}d: {why[:80]}; "
                f"set back to promoted to retry]")
    write_json_atomic(CANDIDATES, cands)
    # Names its file: anything else staged in the shared checkout stays out.
    # A failed commit used to print "parked" and leave the edit staged, so the
    # next source's `git revert` refused the dirty index (2026-10-20 audit).
    r = _git("add", "--", str(CANDIDATES))
    if r.returncode == 0:
        r = _git("commit", "-q", "-m", f"auto-promote: park {sid} on watchlist after repeated reverts (auto)",
                 "--", str(CANDIDATES))
    if r.returncode != 0:
        _git("checkout", "HEAD", "--", str(CANDIDATES))   # back to the committed file, index clean
        print(f"[{tag}]   could not park {sid}: {r.stderr.strip()[:200]}")
        return f"{sid}: reverted, but parking it on the watchlist failed ({r.stderr.strip()[:200]}); tried again on its next revert"
    print(f"[{tag}]   parked {sid} on watchlist ({PARK_AFTER_REVERTS} reverts in {PARK_WINDOW_DAYS} days)")
    return None


def revert(e: dict, outcome: str, why: str, tag: str) -> tuple[bool, str | None]:
    """Revert e's commit and record it. Returns (reverted, problem line or None).

    `reverted` is said outright: the caller used to infer it from the problem
    text and missed "reverted, but not parked" (2026-10-21 follow-up audit).
    """
    sid, commit = e["id"], e.get("commit", "")
    if not commit:
        _record(e, "revert_failed", f"{why} -- no commit sha in history, not reverted")
        return False, f"{sid}: {why}, but no commit sha to revert"
    print(f"[{tag}]   reverting {commit[:8]}")
    r = _git("revert", commit, "--no-edit")
    if r.returncode != 0:
        _git("revert", "--abort")          # never leave a half-done revert behind
        _record(e, "revert_failed", f"{why} -- git revert failed and was aborted: {r.stderr[:200]}")
        return False, f"{sid}: {why}, and git revert failed (aborted; the source is still live)"
    sha = _git("rev-parse", "HEAD").stdout.strip()
    _record(e, outcome, f"{why} -- reverted {commit[:8]}", sha)
    print(f"[{tag}]   ✓ reverted to {sha[:8]}, history updated")
    parked = None
    if recent_reverts(sid, today_bjt()) >= PARK_AFTER_REVERTS:
        parked = park(sid, why, tag)
    push = _git("push", "origin", "main")
    if push.returncode != 0:
        return True, f"{sid}: reverted locally, but git push failed: {push.stderr[:200]}" + (f"; {parked}" if parked else "")
    return True, parked


def check_validate(e: dict) -> tuple[str, str]:
    """('ok'|'fail'|'error', detail) from the commit-message validator."""
    try:
        proc = subprocess.run(["python3", VALIDATOR, "--commit", e["commit"], "--quiet"],
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "error", "validator timed out after 30s"
    detail = (proc.stdout or proc.stderr).strip()[:200]
    return {0: "ok", 1: "fail"}.get(proc.returncode, "error"), detail


def check_probe(e: dict) -> tuple[str, str]:
    # A hung probe is "could not be checked", like a validator error: the
    # other promotions are still checked and this one is named (2026-10-15).
    try:
        proc = subprocess.run(["python3", HEALTH, "--source", e["id"], "--dry-run"],
                              capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "error", "health probe timed out after 180s"
    if proc.returncode != 0:
        return "fail", (proc.stderr or "")[-200:]
    # The validate phase only reads what the agent wrote in its commit
    # message; this is the measurement. Until 2026-10-18 a probe "pass" was
    # >=1 article and >=100 chars, below the gate the message must claim.
    m = re.search(rf"^\s*{re.escape(e['id'])}\s+(\d+) articles · (\d+) chars", proc.stdout or "", re.M)
    if not m:
        return "error", "health probe passed but printed no article/char counts"
    articles, chars = int(m.group(1)), int(m.group(2))
    if articles < MIN_ARTICLES or chars < MIN_CONTENT_CHARS:
        return "fail", (f"measured {articles} articles / {chars} chars, below the gate "
                        f"({MIN_ARTICLES} / {MIN_CONTENT_CHARS})")
    return "ok", f"{articles} articles / {chars} chars"


PHASES = {
    "validate": ("validate-msg", check_validate, "failed_validation",
                 "Commit-msg evidence validation FAIL"),
    "probe": ("probe", check_probe, "auto_reverted", "Post-commit health probe FAIL"),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--phase", choices=PHASES, required=True)
    a = ap.parse_args(argv)
    tag, check, outcome, why = PHASES[a.phase]

    todays = live_promotions(LOG, today_bjt())
    if not todays:
        print(f"[{tag}] no promotions standing today, nothing to verify")
        return 0
    print(f"[{tag}] checking {len(todays)} promotion(s)...")
    reverted, problems = [], []
    for e in reversed(todays):
        print(f"[{tag}] {e['id']} (commit {e.get('commit', '')[:8]})...")
        if a.phase == "validate" and not e.get("commit"):
            problems.append(f"{e['id']}: no commit sha in history, evidence not checked")
            continue
        verdict, detail = check(e)
        if verdict == "ok":
            print(f"[{tag}]   OK ✓")
            continue
        if verdict == "error":
            problems.append(f"{e['id']}: could not be checked: {detail}")
            continue
        print(f"[{tag}]   FAIL ✗ {detail}")
        did_revert, problem = revert(e, outcome, f"{why}: {detail}", tag)
        if problem:
            problems.append(problem)
        if did_revert:
            reverted.append(e["id"])
    for p in problems:
        print(f"[{tag}] ⚠️ {p}")
    if reverted:
        print(f"[{tag}] ⚠️ reverted {len(reverted)} source(s): {reverted}")
    if not (reverted or problems):
        print(f"[{tag}] all {len(todays)} promotion(s) verified")
    return 1 if (reverted or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
