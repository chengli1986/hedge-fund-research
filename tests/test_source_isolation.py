"""One bad source must not cost the night (2026-09-16 audit, finding B1).

fetch_source's try/except covers only `fetcher(source)`; the loop that parses
what it returned sits outside it and reads art["url"] / art["title"] with [],
not .get. And main() accumulated every article in memory, saving once after
all 42 sources. So a single fetcher whose shape drifts -- a site moves its
headline link into a JS button, a synthesised fetcher regresses -- raised out
of main()'s bare `for source in sources:` loop and the run ended before
save_articles ever ran: the ~40 sources that had already succeeded lost their
articles too, and the sources after the broken one were never fetched.

Two halves of the same failure, so both are fixed here:
  · each source is isolated, so a broken one is recorded and skipped;
  · each source's articles are saved as soon as they are fetched, so work
    already done survives a later crash or the cron wrapper's timeout kill.

A fetcher that RAISES (a timeout, a 500) is a different thing and keeps its
existing handling inside fetch_source: it returns [], records a zero, and the
fetcher-health email reports it next morning. What escalates to a failed run
is malformed data reaching the parse loop -- that is a code-level defect, and
the user's call (2026-09-16) is that it should wake someone the same night.
"""
import json

import pytest

import fetch_articles as fa

GOOD1 = [{"title": "Alpha", "url": "https://a.example.com/1", "date": "2026-09-15"}]
GOOD2 = [{"title": "Beta", "url": "https://b.example.com/1", "date": "2026-09-15"}]
MALFORMED = [{"title": "No link here", "date": "2026-09-15"}]          # no "url"


def _sources(*ids):
    return [{"id": i, "name": i, "short_name": i, "method": "api",
             "url": f"https://{i}.example.com/", "expected_hostname": ""} for i in ids]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(fa, "DATA_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(fa, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(fa, "load_entrypoints", lambda: {})
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"sources": _sources("good1", "broken", "good2")}))
    monkeypatch.setattr(fa, "CONFIG_FILE", cfg)
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setitem(fa.FETCHERS, "good1", lambda s: list(GOOD1))
    monkeypatch.setitem(fa.FETCHERS, "broken", lambda s: list(MALFORMED))
    monkeypatch.setitem(fa.FETCHERS, "good2", lambda s: list(GOOD2))
    return tmp_path


def _run(monkeypatch, argv=("fetch_articles.py",)):
    import sys
    monkeypatch.setattr(sys, "argv", list(argv))
    return fa.main()


def _stored(tmp_path):
    f = tmp_path / "articles.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def test_the_sources_around_a_broken_one_still_reach_the_file(env, monkeypatch):
    _run(monkeypatch)
    titles = sorted(a["title"] for a in _stored(env))
    assert titles == ["Alpha", "Beta"], "a malformed source cost the other sources their night"


def test_a_broken_source_makes_the_run_fail(env, monkeypatch):
    """The user's call: malformed data is a code defect, so cron alerts tonight."""
    assert _run(monkeypatch) == 1


def test_a_clean_run_still_succeeds(env, monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "broken", lambda s: list(GOOD2))
    assert _run(monkeypatch) == 0


def test_the_broken_source_records_a_zero_so_the_health_email_sees_it(env, monkeypatch):
    _run(monkeypatch)
    state = json.loads((env / "inspection_state.json").read_text())
    assert state["broken"]["last_article_count"] == 0
    assert state["broken"]["consecutive_zero_count"] == 1


def test_each_source_is_saved_as_it_is_fetched(env, monkeypatch):
    """Not accumulated to the end: a timeout kill at source 40 must not
    discard the 39 that already worked."""
    saved = []
    real_save = fa.save_articles
    monkeypatch.setattr(fa, "save_articles",
                        lambda arts: saved.append([a["title"] for a in arts]) or real_save(arts))
    _run(monkeypatch)
    assert saved == [["Alpha"], ["Beta"]], f"saved in one batch at the end: {saved}"


def test_work_already_done_survives_a_crash_mid_run(env, monkeypatch):
    def explode(source):
        raise KeyboardInterrupt("cron wrapper SIGTERM")
    monkeypatch.setitem(fa.FETCHERS, "broken", explode)
    with pytest.raises(KeyboardInterrupt):
        _run(monkeypatch)
    assert [a["title"] for a in _stored(env)] == ["Alpha"]


def test_a_dry_run_saves_nothing_even_when_a_source_is_broken(env, monkeypatch):
    _run(monkeypatch, ("fetch_articles.py", "--dry-run"))
    assert _stored(env) == []
    assert not (env / "inspection_state.json").exists()


def test_a_fetcher_that_raises_is_not_a_failed_run(env, monkeypatch):
    """A timeout or a 500 is the site, not our code: fetch_source already
    catches it, records a zero, and the health email reports it."""
    def timeout(source):
        raise TimeoutError("read timed out")
    monkeypatch.setitem(fa.FETCHERS, "broken", timeout)
    assert _run(monkeypatch) == 0
    assert sorted(a["title"] for a in _stored(env)) == ["Alpha", "Beta"]
