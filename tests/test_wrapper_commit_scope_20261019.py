"""Wrapper commits name their files (2026-10-19 code review, CDX side-effect).

The wrappers run in the shared checkout and push right after committing. A bare
`git commit -m ...` takes everything staged, so a half-staged change from another
session would have been committed and pushed with the wrapper's own file. Every
commit in scripts/*.sh now ends in `-- <paths>` (git's --only mode: just those
paths, the rest of the index left as it was), and the "anything to commit?"
check before it looks at the same paths.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))


def _commands(text: str) -> list[str]:
    """Shell lines with backslash continuations joined, comments dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l for l in joined.splitlines() if not l.lstrip().startswith("#")]


def _commit_calls() -> list[tuple[str, str]]:
    calls = []
    for path in SCRIPTS:
        for line in _commands(path.read_text()):
            for m in re.finditer(r"\bgit\s+(?:-C\s+\S+\s+)?commit\s+-[^;|&]*", line):
                calls.append((path.name, m.group(0)))
    return calls


def test_the_scan_finds_the_known_commits():
    names = {name for name, _ in _commit_calls()}
    assert {"wrapper-candidate-discovery.sh", "wrapper-fetcher-synthesis.sh",
            "wrapper-profile-refresh.sh"} <= names
    assert len(_commit_calls()) >= 6


def test_every_wrapper_commit_names_its_files():
    bare = [(name, call) for name, call in _commit_calls()
            if not re.search(r"\s--\s+\S", call)]
    assert not bare, bare


def test_the_nothing_to_commit_check_looks_at_the_same_files():
    for path in SCRIPTS:
        for line in _commands(path.read_text()):
            m = re.search(r"git diff --cached --quiet(.*?)\|\|\s*git commit\b.*?\s--\s+(.+)$", line)
            if m:
                assert m.group(1).strip().startswith("--"), (path.name, line)
                checked = m.group(1).strip()[2:].split()
                committed = m.group(2).split()
                assert checked == committed, (path.name, line)


def test_a_scoped_commit_leaves_other_staged_changes_alone(tmp_path):
    run = lambda *a: subprocess.run(a, cwd=tmp_path, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    for f in ("mine.json", "theirs.py"):
        (tmp_path / f).write_text("0")
    run("git", "add", ".")
    run("git", "commit", "-qm", "base")
    (tmp_path / "mine.json").write_text("1")
    (tmp_path / "theirs.py").write_text("half done")
    run("git", "add", "theirs.py")          # another session's staged work
    run("git", "add", "mine.json")
    out = run("bash", "-c", 'git diff --cached --quiet -- mine.json '
                            '|| git commit -q -m "auto" -- mine.json')
    assert out.returncode == 0, out.stderr
    assert run("git", "show", "--name-only", "--format=", "HEAD").stdout.split() == ["mine.json"]
    assert run("git", "diff", "--cached", "--name-only").stdout.split() == ["theirs.py"]


def _wrapper_text() -> str:
    return (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()


def _guard_commit_block() -> str:
    """The discovery wrapper's guard staging + commit lines, as written.

    Cut at the `fi` that closes the stamp commit, not at the first `fi`: an
    `if` added between GUARD_FILES= and the commits would otherwise end the
    block before them (2026-10-21 follow-up audit)."""
    text = _wrapper_text()
    m = re.search(r"^(\s*GUARD_FILES=.*?guard: stamp added_at[^\n]*\n\s*fi\n)", text, re.S | re.M)
    assert m, "guard commit block not found"
    return m.group(1)


def _run_guard_block(tmp_path, track_entrypoints: bool):
    run = lambda *a: subprocess.run(a, cwd=tmp_path, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fund_candidates.json").write_text("0")
    (tmp_path / "theirs.py").write_text("0")
    if track_entrypoints:
        (tmp_path / "config" / "candidate_entrypoints.json").write_text("0")
    run("git", "add", ".")
    run("git", "commit", "-qm", "base")
    (tmp_path / "config" / "fund_candidates.json").write_text("1")
    if track_entrypoints:
        (tmp_path / "config" / "candidate_entrypoints.json").write_text("1")
    (tmp_path / "theirs.py").write_text("half done")
    run("git", "add", "theirs.py")
    out = run("bash", "-c", "GUARD_N=0\n" + _guard_commit_block())
    return run, out


def test_guard_commit_works_without_entrypoints_file(tmp_path):
    # 2026-10-20 review: the file may be absent; the commit must not fail then.
    run, out = _run_guard_block(tmp_path, track_entrypoints=False)
    assert out.returncode == 0, out.stderr
    assert run("git", "log", "-1", "--format=%s").stdout.strip() == "guard: stamp added_at/status_since (auto)"
    assert run("git", "show", "--name-only", "--format=", "HEAD").stdout.split() == ["config/fund_candidates.json"]
    assert run("git", "diff", "--cached", "--name-only").stdout.split() == ["theirs.py"]


def test_guard_commit_includes_entrypoints_when_present(tmp_path):
    run, out = _run_guard_block(tmp_path, track_entrypoints=True)
    assert out.returncode == 0, out.stderr
    assert sorted(run("git", "show", "--name-only", "--format=", "HEAD").stdout.split()) == [
        "config/candidate_entrypoints.json", "config/fund_candidates.json"]
    assert run("git", "diff", "--cached", "--name-only").stdout.split() == ["theirs.py"]
