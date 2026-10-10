"""Stage-3 health check follow-ups (2026-10-10), each reproduced before the fix.

F1  an article every model fails on in the same way was retried every night,
    paid every night, forever; three such at the front stopped the stage
F2  tagging calls were booked in the stage-3 usage log (1,890 / 4,237 rows on
    10-07 / 10-08), and importing analyze_articles sent every importer's log
    lines to analyze_articles.log
F3  refine_dates replaced a precise listing day with a fallback (7 Wellington
    rows: the visible label is "August 2026", the datetime attribute the day)
F4  Ctrl-C during a tag run kept every queued call going and saved nothing
F5  two timeouts on the wording re-ask turned an article into grounding_failed
F6  --retag-before never gave up on a row that kept failing
Fd  resummarize reported a row it did not write as "replaced" (in
    test_resummarize_long_articles.py)
Fe  a passage with no letters passed the evidence check
"""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

import analyze_articles as aa
import publish
import taxonomy
from test_stage3_error_handling import (LATIN, SUMMARY, TAG_BODY, TAG_GOOD, TOO_MANY, USAGE, _art,  # noqa: F401
                                        _distinct, _rows, _tread, _trow, api, http, ok, store, ta,
                                        tagstore)

REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load("refine_dates_f", "scripts/refine_dates.py")


# ── F1: a give-up cap for articles that fail the same way every night ─────────

def _nights(n, capsys):
    out = []
    for _ in range(n):
        rc = aa.main()
        out.append((rc, capsys.readouterr().out))
    return out


def test_f1_a_deterministic_failure_is_given_up_after_max_nights(store, api, capsys):
    store([_art(1)])
    api["script"] = [http(400, "invalid_prompt")]
    nights = _nights(aa.MAX_ANALYSIS_NIGHTS, capsys)
    assert _rows()["a1"]["analysis_failures"] == aa.MAX_ANALYSIS_NIGHTS
    assert "GAVE UP" in nights[-1][1] and all("GAVE UP" not in o for _, o in nights[:-1])
    assert all(rc != 0 for rc, _ in nights)
    assert aa._should_analyze(_rows()["a1"]) is False
    calls = len(api["calls"])
    assert aa.main() == 0 and len(api["calls"]) == calls        # not asked again


def test_f1_the_give_up_night_alerts_even_when_others_were_summarised(store, api, capsys):
    store([_art(1, analysis_failures=aa.MAX_ANALYSIS_NIGHTS - 1), _art(2)],
          bodies={"a1": _distinct(1), "a2": _distinct(2)})
    api["script"] = [http(400, "invalid_prompt")] * (2 * aa.MAX_ATTEMPTS) + [ok(json.dumps(SUMMARY))]
    assert aa.main() == aa.GAVE_UP_RC
    assert "GAVE UP" in capsys.readouterr().out and _rows()["a2"]["summarized"] is True


@pytest.mark.parametrize("script", [[ok("not json")], [http(400, "invalid_prompt")]])
def test_f1_unparsable_answers_and_4xx_count(store, api, script):
    store([_art(1)])
    api["script"] = script
    aa.main()
    assert _rows()["a1"]["analysis_failures"] == 1


@pytest.mark.parametrize("script", [[http(503, "server_error")], [requests.Timeout("slow")],
                                    [http(429, "rate_limit_exceeded")]])
def test_f1_a_night_of_passing_faults_does_not_count(store, api, script):
    store([_art(1)])
    api["script"] = script
    aa.main()
    assert "analysis_failures" not in _rows()["a1"]


def test_f1_a_success_clears_the_count(store, api):
    store([_art(1, analysis_failures=2)])
    api["script"] = [ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    assert _rows()["a1"]["summarized"] is True and "analysis_failures" not in _rows()["a1"]


def test_f1_given_up_articles_no_longer_block_the_ones_behind(store, api):
    rows = [_art(i, analysis_failures=aa.MAX_ANALYSIS_NIGHTS) for i in range(3)] + [_art(9)]
    store(rows, bodies={f"a{i}": _distinct(i) for i in (0, 1, 2, 9)})
    api["script"] = [ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    assert _rows()["a9"]["summarized"] is True and len(api["calls"]) == 1


# ── F5: a re-ask that never got an answer is not a rejection ─────────────────

WORDING = dict(SUMMARY, summary_en="The provided text says the Federal Reserve kept interest rates unchanged.")


def test_f5_two_timed_out_reasks_leave_the_article_pending(api):
    api["script"] = [ok(json.dumps(WORDING)), requests.Timeout("slow"),
                     ok(json.dumps(WORDING)), requests.Timeout("slow")]
    faults = []
    assert aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x", faults=faults) is None
    assert len(api["calls"]) == 4, "the weaker tier must not be asked"
    assert faults and all(f == aa.PASSING for f in faults)


def test_f5_a_reask_that_answers_badly_is_still_a_rejection(api):
    api["script"] = [ok(json.dumps(WORDING)), ok(json.dumps(WORDING))]
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r["insufficient_content"] and r["_label"] == aa.RULE_MADE_DECLINE


# ── F2: each stage keeps its own books and its own log ───────────────────────

def test_f2_summary_rows_say_which_stage_booked_them(tmp_path):
    p = tmp_path / "u.jsonl"
    aa._append_usage_log("x", "gpt-5.6-luna", dict(USAGE), path=p, parsed=True)
    assert json.loads(p.read_text())["stage"] == "summary"


def test_f2_tagging_books_its_calls_in_its_own_log(tagstore, tmp_path, monkeypatch):
    summary_log, tag_log = tmp_path / "summary.jsonl", tmp_path / "tag.jsonl"
    monkeypatch.setattr(aa, "USAGE_LOG_FILE", summary_log)
    monkeypatch.setattr(aa, "TAG_USAGE_LOG_FILE", tag_log)
    path = tagstore([_trow(1)])
    rc = ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None,
                call=lambda p, k, model: (json.dumps(TAG_GOOD), dict(USAGE), model))
    assert rc == 0
    assert not summary_log.exists()
    rows = [json.loads(l) for l in tag_log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["stage"] == "tag" and rows[0]["article_id"] == "t1"


@pytest.mark.parametrize("load", ["import analyze_articles",
                                  "runpy.run_path('scripts/tag_articles.py', run_name='imported')",
                                  "runpy.run_path('scripts/refine_dates.py', run_name='imported')"])
def test_f2_importing_does_not_hijack_the_log(load):
    """Only a summarising run writes analyze_articles.log (refine_dates still logs to
    fetch.log through fetch_articles: stage 1b is stage-1 work)."""
    code = ("import logging, runpy, sys; sys.path[:0] = ['.', 'scripts']; " + load + "; "
            "print(any(getattr(h, 'baseFilename', '').endswith('analyze_articles.log') "
            "for h in logging.getLogger().handlers))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip().splitlines()[-1] == "False"


def test_f2_the_model_switch_check_reads_summary_rows_only():
    text = (REPO / "scripts" / "check-model-switch.sh").read_text(encoding="utf-8")
    assert 'r.get("stage", "summary") == "summary"' in text


# ── F3: a precise listing day is not "month-only" ────────────────────────────

def _drow(date, raw="August 2026", **kw):
    return dict({"id": "d1", "source_id": "wellington", "url": "https://x/1", "date": date,
                 "date_raw": raw, "fetched_at": "2026-09-15T04:00:00+08:00"}, **kw)


@pytest.mark.parametrize("date,expected", [("2026-08-17", False), ("2026-08-31", True), ("2026-08-01", True)])
def test_f3_only_a_month_boundary_date_is_refined(date, expected):
    assert bool(rd.candidates([_drow(date)])) is expected


def test_f3_a_day_from_the_listing_is_shown_as_a_day():
    row = _drow("2026-08-17", date_listed="2026-08-17", date_basis="listing")
    assert publish._display_date(row) == "2026-08-17"


# ── F4: an interrupted tag run stops spending and keeps what it has ──────────

def test_f4_ctrl_c_cancels_the_queue_and_saves_the_finished(tagstore, tmp_path):
    path = tagstore([_trow(i) for i in range(30)])
    calls = []

    def call(prompt, key, model):
        calls.append(1)
        time.sleep(0.02)                 # a real call takes time; the queue must not drain at once
        return json.dumps(TAG_GOOD), dict(USAGE), model

    seen, at_interrupt = [], []

    def log_usage(*a, **k):
        seen.append(1)
        if len(seen) == 5:
            at_interrupt.append(len(calls))
            raise KeyboardInterrupt

    workers = 2
    with pytest.raises(KeyboardInterrupt):
        ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, workers=workers, call=call, log_usage=log_usage)
    # The workers may have run ahead before the interrupt (the fake call is
    # instant); after it, at most the ones already in flight may finish.
    assert len(calls) <= at_interrupt[0] + workers, (at_interrupt, len(calls))
    assert len(calls) < 30
    tagged = [r for r in _tread(path).values() if r.get("tags")]
    assert len(tagged) >= 4, "finished articles must be saved"


# ── F6: re-tagging gives up too ──────────────────────────────────────────────

def test_f6_retag_skips_a_row_that_has_failed_max_nights(tagstore):
    rows = [_trow(1, tags=["research"], tags_at="2026-10-01T00:00:00+08:00", tag_failures=ta.MAX_TAG_NIGHTS),
            _trow(2, tags=["research"], tags_at="2026-10-01T00:00:00+08:00")]
    tagstore(rows)                       # bodies exist: only the failure count can exclude t1
    assert [r["id"] for r in ta.candidates(rows, retag_before="2026-10-09T00:00:00+08:00")] == ["t2"]


def test_f6_retag_failures_are_given_up_once(tagstore, tmp_path):
    path = tagstore([_trow(1, tags=["research"], tags_at="2026-10-01T00:00:00+08:00")])

    def bad(prompt, key, model):
        return json.dumps(TOO_MANY), dict(USAGE), model
    rcs = [ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, call=bad, log_usage=lambda *a, **k: None,
                  retag_before="2026-10-09T00:00:00+08:00") for _ in range(ta.MAX_TAG_NIGHTS + 2)]
    assert rcs == [1, 1, ta.GAVE_UP_RC, 0, 0]
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS


# ── Fe: a passage needs letters ──────────────────────────────────────────────

def test_fe_a_passage_without_letters_is_not_evidence():
    answer = dict(TAG_GOOD, evidence=dict(TAG_GOOD["evidence"], equities="_ _ _ _ _ _ _ _ _ _"))
    kept, dropped = taxonomy.check_evidence(answer, TAG_BODY)
    assert "equities" not in kept["assets"] and any(d.startswith("equities") for d in dropped)
