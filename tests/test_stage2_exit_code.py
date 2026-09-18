"""Stage 2 must be able to fail (audit A1).

run_pipeline.sh reads each stage's exit code: stage 1 returns 1 when no source
produced a single article, stage 3 returns 1 when nothing was summarised at
all, and the cron wrapper emails on a non-zero pipeline. fetch_content.py had
`def main() -> None` and `__main__` called it without sys.exit, so stage 2's
exit code was 0 no matter what it found -- an uncaught exception was the only
way it could fail. A night where every content fetch failed (DNS down, a shared
CDN blocking us, a bug in our own extraction) printed "Pending: 20 | Success: 0"
and the pipeline still reported "all stages OK".

The floor is not a count. The first version was "5 or more pending and none
fetched", and replaying it over all 186 stage-2 runs in logs/fetch_content.log
(2026-03-31 to 2026-09-17) it would have alarmed 14 times -- every one false:
  · 13 were the May-July retry backlog: 5 to 32 articles pending, all of them
    known-bad retries (AQR, Apollo, D. E. Shaw, PineBridge, Robeco, ...), and
    NOT ONE article attempted for the first time that night;
  · 1 was 2026-03-31: 10 new ARK white papers, all behind Cloudflare -- one
    blocked source with a backlog.
Failures cluster by source and retries inflate the pending count, so neither
says anything about whether tonight's fetching works.

An outage is a shared cause across independent sites. So the rule is: the
articles attempted for the FIRST time tonight come from at least two different
sources, and nothing at all was fetched. Replayed over the same 186 runs: zero
alarms. Sensitivity, by assuming every first attempt failed on each of the 171
cron nights: it would have fired on 120 (70%); the rest had no new articles
(34), or new articles from a single source -- nights where an outage loses
nothing stage 2 can report, and stage 1's own floor catches a network outage
first anyway.
"""
import json
import os
import sys

import pytest

import fetch_content as fc

SRC = "aqr"


def _art(i, source=SRC, **kw):
    return dict({"id": f"c{i}", "source_id": source, "title": f"T{i}",
                 "url": f"https://www.{source}.example/{i}", "date": "2026-09-16",
                 "summarized": False}, **kw)


def _run(tmp_path, monkeypatch, rows, fetcher):
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(fc, "DATA_FILE", data)
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(fc, "BASE_DIR", tmp_path)
    for src in {r["source_id"] for r in rows}:
        monkeypatch.setitem(fc.CONTENT_FETCHERS, src, fetcher)
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


def test_new_articles_from_two_sources_all_failing_fails_the_stage(tmp_path, monkeypatch):
    rows = [_art(1, "aqr"), _art(2, "gmo")]
    assert _run(tmp_path, monkeypatch, rows, _fails) == 1


def test_one_blocked_source_with_a_backlog_is_not_an_outage(tmp_path, monkeypatch):
    """2026-03-31: ten new ARK white papers, all behind Cloudflare."""
    rows = [_art(i, "ark-invest") for i in range(10)]
    assert _run(tmp_path, monkeypatch, rows, _fails) in (0, None)


def test_a_retry_backlog_across_many_sources_is_not_an_outage(tmp_path, monkeypatch):
    """May-July: up to 32 known-bad retries from 7 sources, no new article."""
    rows = [_art(i, src, content_attempts=3, content_status="failed")
            for i, src in enumerate(["aqr", "apollo", "robeco", "gsam", "matthews", "des", "pinebridge"])]
    assert _run(tmp_path, monkeypatch, rows, _fails) in (0, None)


def test_any_success_means_fetching_works(tmp_path, monkeypatch):
    """A retry that succeeds proves the network and the extraction both work."""
    rows = [_art(1, "aqr"), _art(2, "gmo"), _art(3, "robeco", content_attempts=2, content_status="failed")]

    def only_the_retry(a):
        return _succeeds(a) if a["source_id"] == "robeco" else _fails(a)

    assert _run(tmp_path, monkeypatch, rows, only_the_retry) in (0, None)


def test_a_clean_run_returns_zero(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, [_art(1, "aqr"), _art(2, "gmo")], _succeeds) in (0, None)


def test_an_empty_queue_is_not_an_outage(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, [], _fails) in (0, None)


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


def test_the_rule_needs_independent_sources():
    assert fc.CONTENT_OUTAGE_MIN_SOURCES >= 2


def test_the_pipeline_reads_stage_2s_exit_code():
    """run_pipeline.sh already branches on it; this pins that it still does."""
    script = (fc.BASE_DIR / "run_pipeline.sh").read_text(encoding="utf-8")
    assert "if python3 fetch_content.py; then" in script
    assert "Stage2:content" in script
