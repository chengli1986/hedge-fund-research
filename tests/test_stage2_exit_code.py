"""Stage 2 must be able to fail (audit A1).

run_pipeline.sh reads each stage's exit code: stage 1 returns 1 when no source
produced a single article, stage 3 returns 1 when nothing was summarised at
all, and the cron wrapper emails on a non-zero pipeline. fetch_content.py had
`def main() -> None` and `__main__` called it without sys.exit, so stage 2's
exit code was 0 no matter what it found -- an uncaught exception was the only
way it could fail. A night where every content fetch failed (DNS down, a shared
CDN blocking us, a bug in our own extraction) printed "Pending: 20 | Success: 0"
and the pipeline still reported "all stages OK".

The floor is deliberately not "any failure": the observed runs have had
pending counts of 1, 14, 18, 20, 21, 25, and a single pending article failing
because one site is down is an ordinary night the fetcher-health email already
reports. CONTENT_OUTAGE_MIN_PENDING is a judgement, not a measurement -- there
is not enough history of all-failed nights to fit a number to -- so it is set
where a normal night cannot reach it: five or more articles queued and not one
of them fetched.
"""
import json
import os
import sys

import pytest

import fetch_content as fc

SRC = "aqr"


def _art(i, **kw):
    return dict({"id": f"c{i}", "source_id": SRC, "title": f"T{i}", "url": f"https://www.aqr.com/{i}",
                 "date": "2026-09-16", "summarized": False}, **kw)


def _run(tmp_path, monkeypatch, n, fetcher):
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps(_art(i)) + "\n" for i in range(n)))
    monkeypatch.setattr(fc, "DATA_FILE", data)
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(fc, "BASE_DIR", tmp_path)
    monkeypatch.setitem(fc.CONTENT_FETCHERS, SRC, fetcher)
    monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
    return fc.main()


def _fails(a):
    fc.note_failure_hint("fetch_error", "connection refused")
    return None


def _succeeds(a):
    p = fc.CONTENT_DIR / f"{a['id']}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("body " * 200)
    return (p, "ok")


def test_a_night_where_nothing_could_be_fetched_fails_the_stage(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, 6, _fails) == 1


def test_one_success_is_not_an_outage(tmp_path, monkeypatch):
    calls = {"n": 0}

    def mixed(a):
        calls["n"] += 1
        return _succeeds(a) if calls["n"] == 1 else _fails(a)

    assert _run(tmp_path, monkeypatch, 6, mixed) in (0, None)


def test_a_handful_of_failures_is_an_ordinary_night(tmp_path, monkeypatch):
    """One site down with two articles queued is what the health email is for."""
    assert _run(tmp_path, monkeypatch, 2, _fails) in (0, None)


def test_a_clean_run_returns_zero(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, 3, _succeeds) in (0, None)


def test_an_empty_queue_is_not_an_outage(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, 0, _fails) in (0, None)


def test_the_module_really_exits_with_mains_return_value(tmp_path):
    """Run it as the pipeline runs it.

    The first version of this test grepped the source for "sys.exit(" and
    passed while the module had no `import sys` at all -- the script raised
    NameError the moment it was run. A test that reads the code instead of
    running it shares the code's blind spot.
    """
    import shutil
    import subprocess
    # A copy, so BASE_DIR (derived from __file__) points at tmp_path and the
    # run writes its logs there instead of into the repo's production logs --
    # which is what conftest's write guard catches.
    script = tmp_path / "fetch_content.py"
    shutil.copy(fc.__file__, script)
    env = dict(os.environ, PYTHONPATH=str(fc.BASE_DIR))
    out = subprocess.run([sys.executable, str(script), "--dry-run"],
                         capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert "NameError" not in out.stderr, out.stderr[-500:]
    assert out.returncode == 0, out.stderr[-500:]

    # And the return value must actually reach the shell: a copy whose main()
    # returns 3 must exit 3. Without sys.exit the process exits 0 and every
    # outage floor above is decoration.
    marked = tmp_path / "fetch_content_marked.py"
    src = script.read_text(encoding="utf-8")
    marked.write_text(src.replace("def main() -> int:\n", "def main() -> int:\n    return 3\n", 1),
                      encoding="utf-8")
    out = subprocess.run([sys.executable, str(marked), "--dry-run"],
                         capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert out.returncode == 3, (out.returncode, out.stderr[-300:])


def test_the_threshold_is_stated_and_above_a_normal_night():
    assert fc.CONTENT_OUTAGE_MIN_PENDING >= 3


def test_the_pipeline_reads_stage_2s_exit_code():
    """run_pipeline.sh already branches on it; this pins that it still does."""
    script = (fc.BASE_DIR / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "if python3 fetch_content.py; then" in script
    assert "Stage2:content" in script
