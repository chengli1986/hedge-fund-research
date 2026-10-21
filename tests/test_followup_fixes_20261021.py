"""Fixes from the 2026-10-21 Claude follow-up audit of adc420da..96da7c0b.

1. verify_promotions: a source whose revert went through but whose parking
   failed (or was skipped over a dirty candidates file) was left out of the
   "reverted N source(s)" line -- the caller guessed from the problem text.
   revert() now says whether it reverted.
2. The discovery wrapper ran the trial manager with no time limit inside a
   960s job; a slow night could take the cron kill into push or report.
3. tests/test_wrapper_commit_scope_20261019 cut the guard block at the first
   `fi`; it worked only because that `fi` happened to close the commit if/else.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
WRAPPER = (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()


# --- 1. revert() reports whether it reverted ----------------------------------
def test_a_reverted_but_unparked_source_is_counted_as_reverted(tmp_path, monkeypatch):
    import verify_promotions as vp
    monkeypatch.setattr(vp, "live_promotions", lambda log, today: [{"id": "x", "commit": "c" * 40}])
    monkeypatch.setattr(vp, "check_probe", lambda e: ("fail", "0 articles"))
    monkeypatch.setitem(vp.PHASES, "probe", ("probe", vp.check_probe, "auto_reverted", "probe FAIL"))
    monkeypatch.setattr(vp, "revert", lambda e, o, w, t: (True, "x: reverted, but not parked"))
    lines = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a))))
    assert vp.main(["--phase", "probe"]) == 1
    assert any("reverted 1 source(s): ['x']" in ln for ln in lines), lines


def test_revert_returns_whether_it_reverted(tmp_path, monkeypatch):
    import verify_promotions as vp
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vp, "_record", lambda *a, **k: None)
    did, problem = vp.revert({"id": "x", "date": "2026-10-21"}, "auto_reverted", "probe FAIL", "probe")
    assert did is False and "no commit sha" in problem


def test_main_keys_on_the_flag_not_on_problem_text():
    src = (ROOT / "scripts" / "verify_promotions.py").read_text()
    assert '"push failed" in problem' not in src


# --- 2. the trial manager has a clock -----------------------------------------
def test_trial_manager_runs_under_timeout_inside_the_job_budget():
    call = re.search(r'^.*gmia-trial-manager\.py" run.*$', WRAPPER, re.M).group(0)
    assert re.search(r'\btimeout\b.*"\$TRIAL_TIMEOUT_S"', call), call
    budget = re.search(r"^TRIAL_TIMEOUT_S=\$\(\( (.+) \)\)$", WRAPPER, re.M).group(1)
    assert "JOB_TIMEOUT_S" in budget and "SECONDS" in budget and "TRIAL_TAIL_RESERVE_S" in budget
    assert re.search(r"^TRIAL_TAIL_RESERVE_S=\d+", WRAPPER, re.M)


def test_a_timed_out_trial_manager_is_named_in_the_log():
    tail = WRAPPER[WRAPPER.index('gmia-trial-manager.py" run'):]
    assert re.search(r'TRIAL_EXIT" -eq 124 \].*\n.*TIMED OUT', tail)


# --- 3. the guard block is cut at the commit's own fi ---------------------------
def test_guard_block_extraction_holds_the_add_and_both_commits():
    from test_wrapper_commit_scope_20261019 import _guard_commit_block
    block = _guard_commit_block()
    assert "git add $GUARD_FILES" in block
    assert block.count("git commit") == 2 and block.rstrip().endswith("fi")


def test_guard_block_extraction_survives_a_nested_if_before_the_commit(monkeypatch):
    import test_wrapper_commit_scope_20261019 as t
    real = (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()
    nested = real.replace('    GUARD_FILES="', '    GUARD_FILES="', 1).replace(
        "    git add $GUARD_FILES 2>/dev/null",
        "    if true; then\n        :\n    fi\n    git add $GUARD_FILES 2>/dev/null", 1)
    monkeypatch.setattr(t, "_wrapper_text", lambda: nested)
    block = t._guard_commit_block()
    assert block.count("git commit") == 2


def test_a_trial_timeout_does_not_exit_124():
    """cron-wrapper reads 124 as its own --timeout firing."""
    tail = WRAPPER[WRAPPER.index('TRIAL_EXIT" -eq 124 ]'):]
    branch = tail[:tail.index("elif")]
    assert re.search(r"^\s*TRIAL_EXIT=5\b", branch, re.M), branch
