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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from promote_today import BJT, live_promotions, today_bjt  # noqa: E402

LOG = Path("logs/auto-promote-history.jsonl")
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


def revert(e: dict, outcome: str, why: str, tag: str) -> str | None:
    """Revert e's commit and record it. Returns a problem line, or None."""
    sid, commit = e["id"], e.get("commit", "")
    if not commit:
        _record(e, "revert_failed", f"{why} -- no commit sha in history, not reverted")
        return f"{sid}: {why}, but no commit sha to revert"
    print(f"[{tag}]   reverting {commit[:8]}")
    r = _git("revert", commit, "--no-edit")
    if r.returncode != 0:
        _git("revert", "--abort")          # never leave a half-done revert behind
        _record(e, "revert_failed", f"{why} -- git revert failed and was aborted: {r.stderr[:200]}")
        return f"{sid}: {why}, and git revert failed (aborted; the source is still live)"
    sha = _git("rev-parse", "HEAD").stdout.strip()
    _record(e, outcome, f"{why} -- reverted {commit[:8]}", sha)
    print(f"[{tag}]   ✓ reverted to {sha[:8]}, history updated")
    push = _git("push", "origin", "main")
    if push.returncode != 0:
        return f"{sid}: reverted locally, but git push failed: {push.stderr[:200]}"
    return None


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
    return ("ok" if proc.returncode == 0 else "fail"), (proc.stderr or "")[-200:]


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
        problem = revert(e, outcome, f"{why}: {detail}", tag)
        if problem:
            problems.append(problem)
        if not problem or "push failed" in problem:
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
