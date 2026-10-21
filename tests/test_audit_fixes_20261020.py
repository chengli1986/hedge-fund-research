"""Two fixes from the 2026-10-20 Claude re-audit of the 10-19 range.

1. The discovery wrapper never looked at the status guard's exit code. A crash
   left GUARD_N empty, `${GUARD_N:-0}` read it as "0 illegal changes", and the
   agent's edits were committed and pushed unchecked. Now a missing snapshot, a
   non-zero exit or a missing "guard: N" line sets GUARD_FAILED: no guard
   commit, push_or_flag holds every push, the report says so, the job exits 4.
2. verify_promotions.park() ignored git add/commit failing (it printed
   "parked" and left the edit staged, which made the next source's
   `git revert` refuse the dirty index) and committed everything staged.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
WRAPPER = (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


# --- 1. guard failure holds the push ------------------------------------------
def _wrapper_part(start: str, end: str) -> str:
    i = WRAPPER.index(start)
    return WRAPPER[i:WRAPPER.index(end, i) + len(end)]


def _run_guard(tmp_path, guard_py: str | None, snapshot: bool = True):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "config").mkdir()
    (repo / "config" / "fund_candidates.json").write_text("[]")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    (repo / "config" / "fund_candidates.json").write_text('[{"id": "x", "status": "promoted"}]')
    _git(repo, "commit", "-qam", "agent edit")            # the agent's own commit
    if guard_py is not None:
        (repo / "guard_candidate_status.py").write_text(guard_py)
    before = tmp_path / "before.json"
    if snapshot:
        before.write_text("[]")
    script = "\n".join([
        f'REPO_DIR="{repo}"; GUARD_BEFORE="{before}"; cd "$REPO_DIR"',
        "ts() { echo T; }",
        _wrapper_part('PUSH_FAILED=""', "\n}\n"),
        _wrapper_part('if [ ! -f "$GUARD_BEFORE" ]; then', 'rm -f "$GUARD_BEFORE"'),
        'push_or_flag "discovery + guard"',
        'echo "GUARD_FAILED=[$GUARD_FAILED] PUSH_FAILED=[$PUSH_FAILED]"',
    ])
    out = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True)
    pushed = _git(remote, "log", "-1", "--format=%s", "main").stdout.strip()
    return out, pushed, _git(repo, "log", "-1", "--format=%s").stdout.strip()


def test_a_crashing_guard_holds_the_push(tmp_path):
    out, pushed, local = _run_guard(tmp_path, "raise SystemExit(1)\n")
    assert out.returncode == 0, out.stderr
    assert "GUARD_FAILED=[guard_candidate_status.py exit 1]" in out.stdout
    assert "held: status guard did not run" in out.stdout
    assert pushed == "base"                       # the unchecked agent edit stayed local
    assert local == "agent edit"                  # and no guard commit was made on top


def test_a_guard_that_exits_0_without_its_line_also_holds_the_push(tmp_path):
    out, pushed, _ = _run_guard(tmp_path, "print('something else')\n")
    assert "GUARD_FAILED=[guard_candidate_status.py exit 0]" in out.stdout
    assert pushed == "base"


def test_no_snapshot_holds_the_push(tmp_path):
    out, pushed, _ = _run_guard(tmp_path, "print('guard: 0 illegal status change(s)')\n", snapshot=False)
    assert "GUARD_FAILED=[no pre-agent snapshot]" in out.stdout
    assert pushed == "base"


def test_a_clean_guard_still_pushes(tmp_path):
    out, pushed, _ = _run_guard(tmp_path, "print('guard: 0 illegal status change(s)')\n")
    assert "GUARD_FAILED=[] PUSH_FAILED=[]" in out.stdout, out.stdout + out.stderr
    assert pushed == "agent edit"


def test_a_failed_guard_is_in_the_report_and_the_exit_code():
    assert "GUARD_FAILED" in re.search(r"^export REPO_DIR .*$", WRAPPER, re.M).group(0)
    assert "{guard_banner}" in WRAPPER and 'os.environ.get("GUARD_FAILED", "")' in WRAPPER
    tail = WRAPPER[WRAPPER.index('if [ "$EXIT_CODE" -ne 0 ]; then\n    echo "$(ts) Exiting'):]
    assert re.search(r'if \[ -n "\$GUARD_FAILED" \]; then\n.*\n    exit 4\n', tail)


# --- 2. park() commits only its file and says when it could not ---------------
def _park_repo(tmp_path, monkeypatch, hook: str | None = None, stage_other: bool = False,
               dirty_candidates: bool = False):
    import verify_promotions as vp
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "config" / "fund_candidates.json").write_text(json.dumps([{"id": "x", "status": "promoted"}]))
    (tmp_path / "config" / "sources.json").write_text('{"sources": []}')
    (tmp_path / "theirs.py").write_text("0")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "config" / "sources.json").write_text('{"sources": [{"id": "x"}]}')
    _git(tmp_path, "commit", "-qam", "promote x")
    sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    if hook:
        h = tmp_path / ".git" / "hooks" / "commit-msg"
        h.write_text(hook)
        h.chmod(0o755)

    def reverts(sid, today, days=vp.PARK_WINDOW_DAYS):
        if stage_other:                          # another session stages work mid-run
            (tmp_path / "theirs.py").write_text("half done")
            _git(tmp_path, "add", "theirs.py")
        if dirty_candidates:                     # someone's uncommitted candidate edit
            f = tmp_path / "config" / "fund_candidates.json"
            f.write_text(json.dumps(json.loads(f.read_text()) + [{"id": "y", "status": "candidate"}]))
        return vp.PARK_AFTER_REVERTS
    monkeypatch.setattr(vp, "recent_reverts", reverts)
    _, problem = vp.revert({"id": "x", "date": vp.today_bjt(), "commit": sha}, "auto_reverted", "probe FAIL", "probe")
    c = json.loads((tmp_path / "config" / "fund_candidates.json").read_text())[0]
    return c, problem


def test_a_failed_park_commit_is_reported_and_leaves_the_index_clean(tmp_path, monkeypatch):
    hook = '#!/bin/sh\ngrep -q "park" "$1" && { echo "hook says no" >&2; exit 1; }\nexit 0\n'
    c, problem = _park_repo(tmp_path, monkeypatch, hook=hook)
    assert "parking it on the watchlist failed" in (problem or "")
    assert "hook says no" in problem
    assert c["status"] == "promoted"              # file back to the committed version
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert _git(tmp_path, "log", "-1", "--format=%s").stdout.startswith("Revert")


def test_park_commits_only_the_candidates_file(tmp_path, monkeypatch):
    c, problem = _park_repo(tmp_path, monkeypatch, stage_other=True)
    assert c["status"] == "watchlist"
    assert "parking" not in (problem or "")       # only the missing remote is reported
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout.split() == [
        "config/fund_candidates.json"]
    assert _git(tmp_path, "diff", "--cached", "--name-only").stdout.split() == ["theirs.py"]


# --- 2b. park() leaves an uncommitted fund_candidates.json alone (2026-10-21) --
# Codex 10-21: on a failed commit `checkout HEAD` wiped edits that had nothing to
# do with parking; on a good one `git add` swept them into the park commit.
def test_park_skips_and_reports_when_candidates_file_has_uncommitted_edits(tmp_path, monkeypatch):
    c, problem = _park_repo(tmp_path, monkeypatch, dirty_candidates=True)
    assert c["status"] == "promoted"              # not parked
    assert "uncommitted" in (problem or "") and "fund_candidates.json" in problem
    rows = json.loads((tmp_path / "config" / "fund_candidates.json").read_text())
    assert [r["id"] for r in rows] == ["x", "y"]  # the other edit survives
    assert _git(tmp_path, "log", "-1", "--format=%s").stdout.startswith("Revert")
    assert _git(tmp_path, "diff", "--cached", "--name-only").stdout == ""
