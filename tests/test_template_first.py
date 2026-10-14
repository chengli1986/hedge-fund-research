"""Template first: a new source tries a listing template before any code (2026-10-13).

Until now every new source arrived as a hand-written fetch_<id>, so template
coverage could only fall (19/43 after Hamilton Lane). The synthesis agent now
runs scripts/try_template.py --record first; a pass is stored on the
candidate, the trial measures with it, auto-promote copies it into
sources.json, and the wrapper checks the record rather than the agent's word.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fetch_articles as fa
import listing_templates as lt
import synthesize_fetchers

REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tt = _load("try_template_tf", "scripts/try_template.py")
ctf = _load("check_template_first_tf", "scripts/check_template_first.py")
tm = _load("trial_manager_tf", "gmia-trial-manager.py")

SPEC = {"type": "card_list", "fetch": "requests", "card": "div.card", "link": "a"}
CAND = {"id": "acme", "name": "Acme", "status": "inaccessible",
        "research_url": "https://www.acme.com/insights"}


def _rows(n, host="www.acme.com"):
    return [{"title": f"T{i}", "url": f"https://{host}/a/{i}", "date": "2026-10-01"} for i in range(n)]


# ── the candidate's source and the verdict ───────────────────────────────────

def test_a_candidate_runs_against_its_research_url_and_host():
    src = lt.candidate_source(dict(CAND, listing_template=SPEC))
    assert src["url"] == "https://www.acme.com/insights"
    assert src["expected_hostname"] == "acme.com" and src["listing_template"] == SPEC


def test_listing_url_and_rss_url_override():
    src = lt.candidate_source(dict(CAND, listing_url="https://feeds.acme.com/rss", rss_url="https://x/rss"))
    assert src["url"] == "https://feeds.acme.com/rss" and src["expected_hostname"] == "feeds.acme.com"
    assert src["rss_url"] == "https://x/rss"


@pytest.mark.parametrize("rows,needle", [
    (_rows(2), "only 2"),
    ([dict(r, title="") for r in _rows(3)], "no title"),
    (_rows(3) + _rows(1), "duplicate"),
    (_rows(3, host="elsewhere.com"), "off acme.com"),
])
def test_rows_that_are_not_good_enough_say_why(rows, needle):
    assert any(needle in p for p in lt.judge_rows(rows, "acme.com"))


def test_good_rows_pass():
    assert lt.judge_rows(_rows(5), "acme.com") == []


# ── scripts/try_template.py ──────────────────────────────────────────────────

def _cands(tmp_path, cand=CAND):
    f = tmp_path / "fund_candidates.json"
    f.write_text(json.dumps([cand]))
    return f


def test_a_pass_is_recorded_with_the_template_the_trial_will_use(tmp_path, monkeypatch):
    f = _cands(tmp_path)
    monkeypatch.setattr(tt.lt, "fetch", lambda source: _rows(10))
    assert tt.main(["--candidate", "acme", "--spec", json.dumps(SPEC), "--record",
                    "--candidates-file", str(f)]) == 0
    c = json.loads(f.read_text())[0]
    assert c["listing_template"] == SPEC and c["listing_url"] == CAND["research_url"]
    assert c["expected_hostname"] == "acme.com"
    assert [a["ok"] for a in c["template_attempts"]] == [True]


def test_a_failure_is_recorded_but_sets_no_template(tmp_path, monkeypatch):
    f = _cands(tmp_path)
    monkeypatch.setattr(tt.lt, "fetch", lambda source: _rows(1))
    assert tt.main(["--candidate", "acme", "--spec", json.dumps(SPEC), "--record",
                    "--candidates-file", str(f)]) == 1
    c = json.loads(f.read_text())[0]
    assert "listing_template" not in c
    assert c["template_attempts"][0]["ok"] is False and "only 1" in c["template_attempts"][0]["problems"][0]


def test_a_site_error_is_a_failed_attempt_not_a_crash(tmp_path, monkeypatch):
    f = _cands(tmp_path)

    def boom(source):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(tt.lt, "fetch", boom)
    assert tt.main(["--candidate", "acme", "--spec", json.dumps(SPEC), "--record",
                    "--candidates-file", str(f)]) == 1
    assert "403" in json.loads(f.read_text())[0]["template_attempts"][0]["problems"][0]


def test_an_invalid_spec_is_refused_before_any_fetch(tmp_path, monkeypatch):
    f = _cands(tmp_path)
    monkeypatch.setattr(tt.lt, "fetch", lambda source: pytest.fail("fetched with a bad spec"))
    assert tt.main(["--candidate", "acme", "--spec", '{"type": "carousel"}', "--record",
                    "--candidates-file", str(f)]) == 1
    assert tt.main(["--candidate", "acme", "--spec", "not json",
                    "--candidates-file", str(f)]) == 2


def test_without_record_nothing_is_written(tmp_path, monkeypatch):
    f = _cands(tmp_path)
    before = f.read_text()
    monkeypatch.setattr(tt.lt, "fetch", lambda source: _rows(10))
    tt.main(["--candidate", "acme", "--spec", json.dumps(SPEC), "--candidates-file", str(f)])
    assert f.read_text() == before


# ── the trial measures with the template ─────────────────────────────────────

def test_the_trial_counts_with_the_candidates_template(monkeypatch):
    monkeypatch.setattr(tm, "load_candidates", lambda: [dict(CAND, listing_template=SPEC)])
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})
    seen = {}
    monkeypatch.setattr(lt, "fetch", lambda source: seen.setdefault("src", source) and _rows(7))
    out = tm.count_articles_with_fetcher({"id": "acme"})
    assert out["accessible"] and out["article_count"] == 7 and out["fetcher_used"]
    assert seen["src"]["listing_template"] == SPEC


def test_without_a_template_the_trial_is_unchanged(monkeypatch):
    monkeypatch.setattr(tm, "load_candidates", lambda: [dict(CAND)])
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"acme": lambda s: _rows(4)})
    monkeypatch.setattr(lt, "fetch", lambda source: pytest.fail("template used without a spec"))
    assert tm.count_articles_with_fetcher({"id": "acme"})["article_count"] == 4


# ── synthesis does not re-target a templated candidate ───────────────────────

def test_a_templated_trial_pass_is_not_a_synthesis_target(monkeypatch):
    # A trial-passed candidate with a template goes to auto-promote, not synthesis.
    # An inaccessible one with a template IS a target since 2026-10-14: its
    # template found nothing in the trial, or its session stopped before the
    # status was set (tests/test_stage6_fixes_20261014.py).
    monkeypatch.setattr(synthesize_fetchers, "load_candidates", lambda: [
        dict(CAND, id="other"),
        dict(CAND, id="prio", status="promoted", synthesis_priority=True, listing_template=SPEC)])
    monkeypatch.setattr(synthesize_fetchers, "load_fetcher_ids", lambda: set())
    assert [t["id"] for t in synthesize_fetchers.list_targets()] == ["other"]


# ── production dispatch ──────────────────────────────────────────────────────

def test_a_template_only_source_has_a_listing_fetcher():
    assert fa.listing_fetcher({"id": "no-code-here", "listing_template_active": True,
                               "listing_template": SPEC}) is lt.fetch
    assert fa.listing_fetcher({"id": "no-code-here"}) is None


# ── the wrapper's check reads the record ─────────────────────────────────────

START = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)
LATER = (START + timedelta(minutes=5)).isoformat()


def _processed(**kw):
    return dict(CAND, synthesis_attempted_at=LATER, synthesis_outcome="success", **kw)


def test_processing_without_a_template_attempt_is_a_violation():
    found = ctf.violations([_processed()], ["acme"], START, set())
    assert found and "without a template attempt" in found[0]


def test_an_attempt_from_an_earlier_session_does_not_count():
    old = [{"at": (START - timedelta(days=7)).isoformat(), "ok": False}]
    assert ctf.violations([_processed(template_attempts=old)], ["acme"], START, set())


def test_writing_code_after_a_passing_template_is_a_violation():
    tries = [{"at": LATER, "ok": True}]
    found = ctf.violations([_processed(template_attempts=tries)], ["acme"], START, {"acme"})
    assert found and "template passed" in found[0]


def test_code_after_failed_templates_needs_a_reason():
    tries = [{"at": LATER, "ok": False}]
    assert ctf.violations([_processed(template_attempts=tries)], ["acme"], START, {"acme"})
    assert ctf.violations([_processed(template_attempts=tries,
                                      synthesis_template_skip_reason="needs two clicks per page")],
                          ["acme"], START, {"acme"}) == []


def test_a_target_the_agent_never_touched_is_not_its_business():
    assert ctf.violations([dict(CAND)], ["acme"], START, set()) == []


def test_the_fetcher_list_is_read_from_the_real_file():
    assert ctf.fetcher_ids(REPO / "fetch_articles.py") == set(fa.FETCHERS)


# ── auto-promote takes a templated candidate ─────────────────────────────────

def test_auto_promote_selection_accepts_a_template():
    text = (REPO / "scripts" / "wrapper-auto-promote.sh").read_text()
    assert 'c["id"] in FETCHERS or c.get("listing_template")' in text
    playbook = (REPO / "auto-promote" / "program.md").read_text()
    assert "listing_template_active" in playbook and "listing_fetcher(src)" in playbook
