"""The discovery agent commits; the wrapper pushes after the guard (2026-10-17).

The agent added seed ariel-investments without added_at and pushed it itself
(d63457c). CI failed test_every_candidate_on_disk_has_added_at until the
guard's stamp reached GitHub in a later commit (13e140f). Every new seed would
have mailed one CI failure.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()
PROGRAM = (ROOT / "candidate-discovery" / "program.md").read_text()


def _no_push_env() -> dict:
    """NO_PUSH_ENV as the wrapper defines it (one source of truth)."""
    m = re.search(r"NO_PUSH_ENV=\((.*?)\)", WRAPPER, re.S)
    assert m, "NO_PUSH_ENV not defined in the wrapper"
    return dict(pair.split("=", 1) for pair in m.group(1).split())


def test_the_agent_is_started_with_push_disabled():
    assert re.search(r'env "\$\{NO_PUSH_ENV\[@\]\}" timeout [^\n]*"\$CLAUDE_BIN" -p', WRAPPER)


def test_the_first_push_comes_after_the_guard_and_its_commit():
    guard = WRAPPER.index("python3 guard_candidate_status.py")
    stamp_commit = WRAPPER.index('git commit -m "guard: stamp added_at/status_since (auto)"')
    # pushes go through push_or_flag (2026-10-18); its body is the only bare one
    first_push = WRAPPER.index('push_or_flag "')
    assert guard < stamp_commit < first_push
    assert len(re.findall(r"^\s*(?:if ! )?git push\b", WRAPPER, re.M)) == 1   # only in push_or_flag


def test_the_stamp_commit_does_not_depend_on_a_revert():
    block = WRAPPER[WRAPPER.index("python3 guard_candidate_status.py"):WRAPPER.index('GUARD_REVERTS=')]
    add = block.index("git add config/fund_candidates.json")
    first_if = block.index('if [ "${GUARD_N:-0}" -gt 0 ]')
    assert add < first_if


def test_the_program_no_longer_tells_the_agent_to_push():
    code = "\n".join(re.findall(r"```bash\n(.*?)```", PROGRAM, re.S))
    assert "git push" not in code
    assert "Do **not** `git push`" in PROGRAM
    assert "do NOT push" in WRAPPER


def test_no_push_env_really_stops_git_push_and_only_that(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    run = lambda *a, env=None, cwd=work: subprocess.run(
        a, cwd=cwd, capture_output=True, text=True, env=env)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (work / "f").write_text("1")
    run("git", "add", "f")
    run("git", "commit", "-qm", "c")
    import os
    blocked = run("git", "push", "-q", "origin", "HEAD:main", env={**os.environ, **_no_push_env()})
    assert blocked.returncode != 0
    assert run("git", "rev-parse", "--verify", "-q", "main", cwd=remote).returncode != 0
    ok = run("git", "push", "-q", "origin", "HEAD:main")
    assert ok.returncode == 0, ok.stderr
